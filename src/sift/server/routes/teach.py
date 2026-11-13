"""The embedding model (status and consented download), Teach the app clusters, text search,
and labels."""

import threading
import time
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sift.analysis.embed import ClipOnnx, EmbeddingModel, search
from sift.analysis.models import CLIP, download, installed
from sift.backup.phonepaths import shown
from sift.device.tracker import STORED_PHONE_ORDER
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.vault import TaskOut, start_task
from sift.services.tasks import TaskControl

router = APIRouter()

_models: dict[str, EmbeddingModel] = {}
_models_lock = threading.Lock()


def embedding_model(ctx: Ctx) -> EmbeddingModel | None:
    """The loaded model, or None until it is downloaded. Loaded once per models folder."""
    key = str(ctx.paths.models)
    with _models_lock:
        if key not in _models:
            if not installed(ctx.paths.models):
                return None
            _models[key] = ClipOnnx(ctx.paths.models)
        return _models[key]


def set_model(ctx: Ctx, model: EmbeddingModel | None) -> None:
    """For tests and after a download: use `model` (None forgets the loaded one)."""
    with _models_lock:
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
    return ModelOut(key=CLIP.key, installed=installed(ctx.paths.models), size_bytes=CLIP.size)


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


class ClusterCard(BaseModel):
    id: int
    size: int
    unique: int
    sample_ids: list[int]  # phone_file ids for the 4 x 3 mosaic
    sample_text: str | None
    label: str | None  # set when every member has the same label


class ClustersOut(BaseModel):
    images: int
    covered: int
    clusters: list[ClusterCard]


@router.get("/cleanup/clusters")
def clusters(ctx: Ctx) -> ClustersOut:
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
            )
        )
    cards.sort(key=lambda c: -c.size)
    return ClustersOut(images=images, covered=sum(c.size for c in cards), clusters=cards)


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


class LabelOut(BaseModel):
    labelled: int


@router.post("/labels")
def add_labels(ctx: Ctx, body: LabelRequest) -> LabelOut:
    hashes = list(dict.fromkeys(body.hashes))
    source = "manual"
    if body.cluster_id is not None:
        with ctx.db.read() as conn:
            hashes += [
                str(r[0])
                for r in conn.execute(
                    "SELECT content_hash FROM cluster_member WHERE cluster_id = ?",
                    (body.cluster_id,),
                )
            ]
        source = "cluster"
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
            "WHERE label.label != 'protected'",  # a protected file stays protected
            [(h, body.label, body.tag, source, body.cluster_id, now) for h in known],
        )
    return LabelOut(labelled=len(known))
