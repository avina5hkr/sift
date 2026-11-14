"""The embedding model (status and consented download), Teach the app clusters, text search,
and labels."""

import logging
import threading
import time
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sift.analysis.embed import ClipOnnx, EmbeddingModel, search
from sift.analysis.models import CLIP, discard_bad, download, installed
from sift.backup.phonepaths import shown
from sift.cleanup.junk import bump_labels
from sift.cleanup.suggest import load_teaching, medoid_vectors, suggest
from sift.device.tracker import STORED_PHONE_ORDER
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.vault import TaskOut, start_task
from sift.services.tasks import TaskControl

log = logging.getLogger("sift.analysis")
router = APIRouter()

_models: dict[str, EmbeddingModel] = {}
_models_lock = threading.Lock()
_broken: set[str] = set()  # models folders whose intact files would not load


def embedding_model(ctx: Ctx) -> EmbeddingModel | None:
    """The loaded model, or None until it is downloaded. Loaded once per models folder."""
    key = str(ctx.paths.models)
    with _models_lock:
        if key not in _models:
            if key in _broken or not installed(ctx.paths.models):
                return None
            try:
                _models[key] = ClipOnnx(ctx.paths.models)
            except Exception as e:
                bad = discard_bad(ctx.paths.models)
                log.warning("The model could not be loaded (%s); damaged files: %s", e, bad)
                if not bad:
                    _broken.add(key)  # the files are right but will not load: say so once
                return None
        return _models[key]


def set_model(ctx: Ctx, model: EmbeddingModel | None) -> None:
    """For tests and after a download: use `model` (None forgets the loaded one)."""
    with _models_lock:
        _broken.discard(str(ctx.paths.models))
        if model is None:
            _models.pop(str(ctx.paths.models), None)
        else:
            _models[str(ctx.paths.models)] = model


class ModelOut(BaseModel):
    key: str
    installed: bool
    size_bytes: int


@router.get("/cleanup/models")
def model_status(ctx: Ctx) -> ModelOut:
    ok = installed(ctx.paths.models) and str(ctx.paths.models) not in _broken
    return ModelOut(key=CLIP.key, installed=ok, size_bytes=CLIP.size)


class DownloadRequest(BaseModel):
    consent: bool = False


@router.post("/cleanup/models/download")
def download_model(ctx: Ctx, body: DownloadRequest) -> TaskOut:
    if not body.consent:
        raise ApiError(
            400,
            "consent_required",
            f"Sift needs your permission to download the model ({CLIP.size // 1_000_000} MB).",
        )

    def work(control: TaskControl) -> dict[str, Any]:
        download(
            ctx.paths.models,
            consent=True,
            should_stop=lambda: control.should_stop,
            on_bytes=control.progress,
        )
        set_model(ctx, None)
        return {"installed": installed(ctx.paths.models)}

    return start_task(ctx, "model", work)


def _device_id(ctx: Ctx) -> int:
    with ctx.db.read() as conn:
        row = conn.execute(f"SELECT id FROM device {STORED_PHONE_ORDER} LIMIT 1").fetchone()
    if row is None:
        raise ApiError(409, "no_phone", "Back up a phone first; then Sift can analyse it.")
    return int(row[0])


def _phone_files(ctx: Ctx, device_id: int, hashes: list[str]) -> dict[str, tuple[int, str]]:
    """content hash -> (a present phone_file id, its path) on this phone."""
    if not hashes:
        return {}
    out: dict[str, tuple[int, str]] = {}
    with ctx.db.read() as conn:
        for start in range(0, len(hashes), 500):
            part = hashes[start : start + 500]
            marks = ",".join("?" for _ in part)
            for digest, pid, path in conn.execute(
                f"SELECT content_hash, min(id), remote_path FROM phone_file WHERE device_id = ? "
                f"AND present = 1 AND content_hash IN ({marks}) GROUP BY content_hash",
                (device_id, *part),
            ):
                out[str(digest)] = (int(pid), shown(str(path)))
    return out


class SuggestionOut(BaseModel):
    label: str | None
    reason: str
    source: str


class ClusterCard(BaseModel):
    id: int
    size: int
    unique: int
    sample_ids: list[int]  # phone_file ids for the 4 x 3 mosaic
    sample_text: str | None
    label: str | None  # set when every member has the same label
    marked: int  # members with a label
    suggestion: SuggestionOut | None


class ClustersOut(BaseModel):
    images: int
    covered: int
    to_mark: int  # groups with no label yet
    marked: int  # groups with at least one label
    offset: int
    clusters: list[ClusterCard]


PAGE = 24


@router.get("/cleanup/clusters")
def clusters(
    ctx: Ctx, section: Literal["to_mark", "marked"] = "to_mark", offset: int = 0, limit: int = PAGE
) -> ClustersOut:
    """One page of groups, largest first: those not marked yet, or those marked."""
    device_id = _device_id(ctx)
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT m.cluster_id, m.content_hash, c.ocr_text, l.label, n.group_id "
            "FROM cluster_member m JOIN content c ON c.hash = m.content_hash "
            "LEFT JOIN label l ON l.content_hash = m.content_hash "
            "LEFT JOIN near_member n ON n.content_hash = m.content_hash "
            "ORDER BY m.cluster_id, m.sim DESC"
        ).fetchall()
        images = int(
            conn.execute(
                "SELECT count(*) FROM content WHERE embedding IS NOT NULL AND kind = 'image'"
            ).fetchone()[0]
        )
    by: dict[int, list[tuple[Any, ...]]] = {}
    for row in rows:
        by.setdefault(int(row[0]), []).append(row)
    files = _phone_files(ctx, device_id, [str(r[1]) for r in rows])
    cards = []
    for cid, members in by.items():
        present = [m for m in members if str(m[1]) in files]
        if len(present) < 2:
            continue
        unique = len({(f"n{m[4]}" if m[4] is not None else str(m[1])) for m in present})
        labels = {m[3] for m in present}
        text = next((str(m[2]) for m in present if m[2]), None)
        cards.append(
            ClusterCard(
                id=cid,
                size=len(present),
                unique=unique,
                sample_ids=[files[str(m[1])][0] for m in present[:12]],
                sample_text=text[:140] if text else None,
                label=next(iter(labels)) if len(labels) == 1 and None not in labels else None,
                marked=sum(1 for m in present if m[3] is not None),
                suggestion=None,
            )
        )
    cards.sort(key=lambda c: -c.size)
    todo = [c for c in cards if not c.marked]
    done = [c for c in cards if c.marked]
    chosen = todo if section == "to_mark" else done
    page = chosen[max(offset, 0) : max(offset, 0) + max(1, min(limit, 100))]
    _suggest(ctx, page, {cid: [str(m[1]) for m in by[cid]] for cid in (c.id for c in page)})
    return ClustersOut(
        images=images,
        covered=sum(c.size for c in cards),
        to_mark=len(todo),
        marked=len(done),
        offset=offset,
        clusters=page,
    )


def _suggest(ctx: Ctx, page: list[ClusterCard], members: dict[int, list[str]]) -> None:
    """Fill each card's suggestion: from the owner's teaching when it is close, else built in."""
    model = embedding_model(ctx)
    if model is None or not page:
        return
    vectors = medoid_vectors(ctx.db, [c.id for c in page], model.version)
    teaching = load_teaching(ctx.db, model.version)
    for card in page:
        vec = vectors.get(card.id)
        if vec is None:
            continue
        found = suggest(model, vec, teaching, members[card.id])
        card.suggestion = SuggestionOut(label=found.label, reason=found.reason, source=found.source)


class MediaItem(BaseModel):
    id: int
    path: str
    content_hash: str
    score: float | None = None
    label: str | None = None


@router.get("/cleanup/clusters/{cluster_id}")
def cluster_members(ctx: Ctx, cluster_id: int) -> list[MediaItem]:
    device_id = _device_id(ctx)
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT m.content_hash, m.sim, l.label FROM cluster_member m "
            "LEFT JOIN label l ON l.content_hash = m.content_hash "
            "WHERE m.cluster_id = ? ORDER BY m.sim DESC",
            (cluster_id,),
        ).fetchall()
    if not rows:
        raise ApiError(404, "not_found", "That group does not exist any more. Analyze again.")
    files = _phone_files(ctx, device_id, [str(r[0]) for r in rows])
    return [
        MediaItem(
            id=files[str(h)][0],
            path=files[str(h)][1],
            content_hash=str(h),
            score=float(s),
            label=lab,
        )
        for h, s, lab in rows
        if str(h) in files
    ]


@router.get("/cleanup/search")
def text_search(ctx: Ctx, q: str) -> list[MediaItem]:
    query = q.strip()
    if not query:
        return []
    model = embedding_model(ctx)
    if model is None:
        raise ApiError(409, "no_model", "Download the model first; then you can search by words.")
    device_id = _device_id(ctx)
    found = search(ctx.db, model, query, limit=120)
    files = _phone_files(ctx, device_id, [h for h, _s in found])
    return [
        MediaItem(id=files[h][0], path=files[h][1], content_hash=h, score=s)
        for h, s in found
        if h in files
    ][:60]


class LabelRequest(BaseModel):
    hashes: list[str] = Field(default_factory=list, max_length=20_000)
    cluster_id: int | None = None
    label: Literal["junk", "keep"]
    tag: Literal["greeting", "quote", "meme", "devotional", "other"] | None = None
    source: Literal["manual", "feedback"] = "manual"  # feedback: Not junk on the Junk tab


class LabelOut(BaseModel):
    labelled: int


@router.post("/labels")
def add_labels(ctx: Ctx, body: LabelRequest) -> LabelOut:
    hashes = list(dict.fromkeys(body.hashes))
    members: list[str] = []
    if body.cluster_id is not None:
        with ctx.db.read() as conn:
            members = [
                str(r[0])
                for r in conn.execute(
                    "SELECT content_hash FROM cluster_member WHERE cluster_id = ?",
                    (body.cluster_id,),
                )
            ]
        if not members:
            raise ApiError(409, "cluster_gone", "This group changed; reload Teach the app.")
    manual = set(hashes)
    hashes += [h for h in members if h not in manual]
    known: set[str] = set()
    with ctx.db.read() as conn:
        for start in range(0, len(hashes), 500):
            part = hashes[start : start + 500]
            marks = ",".join("?" for _ in part)
            sql = f"SELECT hash FROM content WHERE hash IN ({marks})"
            known |= {str(r[0]) for r in conn.execute(sql, part)}
    now = int(time.time())
    with ctx.db.tx() as conn:
        conn.executemany(
            "INSERT INTO label (content_hash, label, tag, source, cluster_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (content_hash) DO UPDATE SET "
            "label = excluded.label, tag = excluded.tag, source = excluded.source, "
            "cluster_id = excluded.cluster_id, created_at = excluded.created_at "
            # a protected file stays protected; a group label never replaces a picture's own
            "WHERE label.label != 'protected' AND (excluded.source != 'cluster' "
            "OR label.source = 'cluster')",
            [
                (
                    h,
                    body.label,
                    body.tag,
                    body.source if h in manual else "cluster",
                    body.cluster_id,
                    now,
                )
                for h in known
                if h in manual or h in members
            ],
        )
        bump_labels(conn)  # the junk model retrains on its next use
    return LabelOut(labelled=len(known))


@router.get("/labels/export")
def export_labels(ctx: Ctx) -> dict[str, Any]:
    """Everything taught so far, to keep in a file on this Mac (nothing leaves it)."""
    from sift.cleanup.labels_io import export

    return export(ctx.db)


class ImportOut(BaseModel):
    read: int
    applied: int
    waiting: int  # for pictures Sift has not backed up yet


@router.post("/labels/import")
def import_labels(ctx: Ctx, body: dict[str, Any]) -> ImportOut:
    from sift.cleanup.labels_io import load

    try:
        return ImportOut(**load(ctx.db, body))
    except ValueError as e:
        raise ApiError(422, "not_teaching", str(e)) from e
