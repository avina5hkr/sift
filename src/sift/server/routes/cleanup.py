"""Cleanup: Analyze (index + exact duplicates), Cleanup home summary, the Review list, selection."""

import json
import time
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from sift.analysis.cluster import run_clusters
from sift.analysis.embed import run_embed
from sift.analysis.index import run_index
from sift.analysis.models import CLIP
from sift.analysis.near import NearStopped, find_groups, load_items, store_groups
from sift.analysis.ocr import run_ocr
from sift.backup.phonepaths import shown
from sift.backup.run import usable_dests
from sift.cleanup.candidates import (
    is_checked,
    rebuild_exact,
    select_all_in_tab,
    set_selection,
)
from sift.cleanup.junk import rebuild_junk
from sift.cleanup.near_tabs import TabItem, selectable_ids, tab_groups
from sift.core.origin import classify_origin
from sift.device.tracker import STORED_PHONE_ORDER
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.teach import embedding_model
from sift.server.routes.vault import TaskOut, start_task, tasks_of
from sift.services.tasks import TaskControl

router = APIRouter()

ANALYZED_AT = "cleanup_analyzed_at"
PAGE = 200


def stored_device_id(ctx: Ctx) -> int | None:
    with ctx.db.read() as conn:
        row = conn.execute(f"SELECT id FROM device {STORED_PHONE_ORDER} LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def _device(ctx: Ctx) -> int:
    device_id = stored_device_id(ctx)
    if device_id is None:
        raise ApiError(409, "no_phone", "Back up a phone first; then Sift can analyse it.")
    return device_id


def screen_sizes(ctx: Ctx) -> list[tuple[int, int]]:
    """The phone's screen sizes (`wm size`), cached per phone (plan assumption B2)."""
    session = ctx.tracker.session() if ctx.tracker is not None else None
    with ctx.db.read() as conn:
        row = conn.execute(f"SELECT uuid FROM device {STORED_PHONE_ORDER} LIMIT 1").fetchone()
    uuid = str(row[0]) if row else ""
    key = f"screen_sizes:{uuid}"
    if session is not None and session.device_uuid == uuid:
        try:
            sizes = [list(s) for s in session.transport.info().screen_sizes]
            with ctx.db.tx() as conn:
                conn.execute(
                    "INSERT INTO setting (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(sizes)),
                )
        except Exception:
            pass
    with ctx.db.read() as conn:
        cached = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return [(int(w), int(h)) for w, h in json.loads(cached[0])] if cached else []


@router.post("/cleanup/analyze")
def analyze(ctx: Ctx) -> TaskOut:
    """Index (A), exact and near copies (B-E), OCR (F) and, with the model, embeddings and
    clusters (G, H)."""
    device_id = _device(ctx)
    screens = screen_sizes(ctx)

    def work(control: TaskControl) -> dict[str, Any]:
        stats = run_index(
            ctx.db,
            ctx.paths.cache,
            screens,
            dest_ids={d.id for d in usable_dests(ctx)},
            should_stop=lambda: control.should_stop,
            on_file=control.progress,
        )
        groups = rebuild_exact(ctx.db, device_id)
        stop = lambda: control.should_stop  # noqa: E731
        try:
            found = find_groups(load_items(ctx.db, device_id), ctx.config.analysis, stop)
            near = store_groups(ctx.db, found)
        except NearStopped:  # stopped: the last complete groups stay
            near = []
        dests = {d.id for d in usable_dests(ctx)}
        read = run_ocr(
            ctx.db,
            langs=ctx.config.analysis.ocr_languages,
            threads=ctx.config.analysis.workers,
            dest_ids=dests,
            should_stop=stop,
            on_file=control.progress,
        )
        model = embedding_model(ctx)
        embedded = 0
        clusters: int | None = 0
        if model is not None:
            embedded = run_embed(
                ctx.db,
                model,
                ctx.paths.cache,
                dest_ids=dests,
                should_stop=stop,
                on_file=control.progress,
            )
            clusters = run_clusters(
                ctx.db,
                ctx.config.analysis,
                model.version,
                changed=embedded > 0,
                should_stop=stop,
            )
        partial = {"indexed": stats.indexed, "ocr": read, "embedded": embedded}
        if control.should_stop:  # stopped: the later steps run next time; not marked analysed
            return partial
        from sift.cleanup.labels_io import apply_pending

        apply_pending(ctx.db)  # teaching loaded from a file, for pictures now known
        junk = rebuild_junk(
            ctx.db,
            device_id,
            ctx.config,
            model.version if model is not None else CLIP.version,
            ctx.paths.lexicons,
        )
        with ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (ANALYZED_AT, str(int(time.time()))),
            )
        return {
            "suggested": junk["suggested"],
            "maybe": junk["maybe"],
            "indexed": stats.indexed,
            "unreadable": stats.unreadable,
            "exact_groups": groups,
            "near_groups": len(near),
            "ocr": read,
            "embedded": embedded,
            "clusters": clusters,
        }

    return start_task(ctx, "analyze", work)


@router.post("/cleanup/cancel")
def cancel(ctx: Ctx) -> TaskOut | None:
    runner = tasks_of(ctx)
    runner.stop(timeout_s=0.0)
    status = runner.current()
    return None if status is None else TaskOut(**status.as_event())


Category = Literal["Camera", "WhatsApp images", "WhatsApp video", "Sent", "Other"]


def category(path: str, kind: str) -> Category:
    origin = classify_origin(path)
    if origin == "camera":
        return "Camera"
    if origin == "whatsapp_sent":
        return "Sent"
    if origin.startswith("whatsapp"):
        if kind == "video":
            return "WhatsApp video"
        return "WhatsApp images" if kind == "image" else "Other"  # documents, voice notes
    return "Other"


class Card(BaseModel):
    tab: str
    groups: int
    files: int
    bytes: int


class CleanupSummary(BaseModel):
    has_backup: bool
    analyzed_at: int | None
    new_since: int  # backed-up files not analysed yet
    storage: dict[str, int]  # category -> bytes on the phone
    cards: list[Card]


@router.get("/cleanup/summary")
def summary(ctx: Ctx) -> CleanupSummary:
    device_id = stored_device_id(ctx)
    storage: dict[str, int] = dict.fromkeys(
        ["Camera", "WhatsApp images", "WhatsApp video", "Sent", "Other"], 0
    )
    with ctx.db.read() as conn:
        rows = (
            conn.execute(
                "SELECT p.remote_path, p.size, IFNULL(c.kind, 'other') FROM phone_file p "
                "LEFT JOIN content c ON c.hash = p.content_hash "
                "WHERE p.device_id = ? AND p.present = 1",
                (device_id,),
            ).fetchall()
            if device_id is not None
            else []
        )
        has_backup = bool(conn.execute("SELECT 1 FROM replica LIMIT 1").fetchone())
        at = conn.execute("SELECT value FROM setting WHERE key = ?", (ANALYZED_AT,)).fetchone()
        new_since = int(
            conn.execute(
                "SELECT count(*) FROM content WHERE analysis_version < 1 "
                "AND hash IN (SELECT content_hash FROM replica)"
            ).fetchone()[0]
        )
    groups = exact_groups_now(ctx, device_id) if device_id is not None else []
    for path, size, kind in rows:
        storage[category(shown(str(path)), str(kind))] += int(size)
    return CleanupSummary(
        has_backup=has_backup,
        analyzed_at=int(at[0]) if at else None,
        new_since=new_since,
        storage=storage,
        cards=[
            Card(
                tab="exact",
                groups=len(groups),
                files=sum(len(g.copies) for g in groups),
                bytes=sum(g.reclaimable for g in groups),
            ),
            *(
                Card(
                    tab=tab,
                    groups=len(tg),
                    files=sum(len(g.copies) for g in tg),
                    bytes=sum(c.size for g in tg for c in g.copies),
                )
                for tab in ("near", "similar")
                for tg in [tab_groups(ctx.db, device_id, tab) if device_id is not None else []]
            ),
        ],
    )


class Item(BaseModel):
    id: int
    path: str
    size: int
    mtime: int
    origin: str
    kind: str
    width: int | None
    height: int | None
    checked: bool
    role: str


class GroupOut(BaseModel):
    group_id: int
    content_hash: str
    keeper: Item | None  # None on the Junk tabs: each content is its own group
    copies: list[Item]
    reclaimable: int
    score: float | None = None
    reasons: list[dict[str, str]] = []


class CandidatesOut(BaseModel):
    tab: str
    groups: list[GroupOut]
    total_groups: int
    next_cursor: int | None


def exact_groups_now(ctx: Ctx, device_id: int) -> list[GroupOut]:
    """The exact-duplicate groups as the phone is now: only present files whose content is
    still the keeper's; a group whose keeper is gone or changed is left out until the next
    rebuild (every backup run rebuilds them)."""
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT c.group_id, p.id, p.remote_path, p.size, p.mtime, p.content_hash, c.role, "
            "c.default_checked, s.checked, IFNULL(t.kind, 'other'), t.width, t.height "
            "FROM candidate c JOIN phone_file p ON p.id = c.phone_file_id "
            "LEFT JOIN selection s ON s.phone_file_id = p.id "
            "LEFT JOIN content t ON t.hash = p.content_hash "
            "WHERE c.tab = 'exact' AND p.device_id = ? AND p.present = 1 "
            "AND p.content_hash IS NOT NULL ORDER BY c.group_id, c.role DESC, p.id",
            (device_id,),
        ).fetchall()
    groups: dict[int, GroupOut] = {}
    for gid, pid, path, size, mtime, digest, role, default, chosen, kind, width, height in rows:
        item = Item(
            id=pid,
            path=shown(path),
            size=size,
            mtime=mtime,
            origin=classify_origin(shown(path)),
            kind=kind,
            width=width,
            height=height,
            role=role,
            checked=role != "keeper" and is_checked(default, chosen),
        )
        if role == "keeper":
            groups[gid] = GroupOut(
                group_id=gid, content_hash=digest, keeper=item, copies=[], reclaimable=0
            )
        elif gid in groups and digest == groups[gid].content_hash:
            groups[gid].copies.append(item)
            groups[gid].reclaimable += size
    return [g for g in groups.values() if g.copies]


def near_groups_now(ctx: Ctx, device_id: int, tab: str) -> list[GroupOut]:
    groups = tab_groups(ctx.db, device_id, tab)
    ids = [c.phone_file_id for g in groups for c in g.copies]
    with ctx.db.read() as conn:
        chosen = (
            {
                int(r[0]): int(r[1])
                for r in conn.execute(
                    f"SELECT phone_file_id, checked FROM selection WHERE phone_file_id IN "
                    f"({','.join('?' for _ in ids)})",
                    ids,
                )
            }
            if ids
            else {}
        )

    def item(t: TabItem, role: str, checked: bool) -> Item:
        path = shown(t.path)
        return Item(
            id=t.phone_file_id,
            path=path,
            size=t.size,
            mtime=t.mtime,
            origin=classify_origin(path),
            kind=t.kind,
            width=t.width,
            height=t.height,
            checked=checked,
            role=role,
        )

    out = []
    for g in groups:
        copies = [
            item(c, "copy", is_checked(int(c.default_checked), chosen.get(c.phone_file_id)))
            for c in g.copies
        ]
        out.append(
            GroupOut(
                group_id=g.group_id,
                content_hash=g.keeper.content_hash,
                keeper=item(g.keeper, "keeper", False),
                copies=copies,
                reclaimable=sum(c.size for c in copies),
            )
        )
    return out


@router.get("/cleanup/candidates")
def candidates(ctx: Ctx, tab: str = "exact", sort: str = "size", cursor: int = 0) -> CandidatesOut:
    if tab not in ("exact", "near", "similar", "junk", "maybe"):
        return CandidatesOut(tab=tab, groups=[], total_groups=0, next_cursor=None)
    device_id = _device(ctx)
    if tab in ("junk", "maybe"):
        from sift.server.routes.junk import junk_groups_now

        ordered = junk_groups_now(ctx, device_id, tab)
    elif tab == "exact":
        ordered = exact_groups_now(ctx, device_id)
    else:
        ordered = near_groups_now(ctx, device_id, tab)
    if sort == "date":
        ordered.sort(key=lambda g: -(g.keeper or g.copies[0]).mtime)
    elif sort == "score":
        ordered.sort(key=lambda g: -(g.score or 0))
    elif sort == "group":
        ordered.sort(key=lambda g: -len(g.copies))
    else:
        ordered.sort(key=lambda g: -g.reclaimable)
    page = ordered[cursor : cursor + PAGE]
    nxt = cursor + PAGE if cursor + PAGE < len(ordered) else None
    return CandidatesOut(tab=tab, groups=page, total_groups=len(ordered), next_cursor=nxt)


class SelectionRequest(BaseModel):
    ids: list[int] = []
    checked: bool
    all_in_tab: str | None = None


class SelectionOut(BaseModel):
    changed: int


@router.post("/selection")
def selection(ctx: Ctx, body: SelectionRequest) -> SelectionOut:
    device_id = _device(ctx)
    from sift.server.routes.junk import fresh

    fresh(ctx, device_id)  # a file that became Protected or Keep can no longer be ticked
    if body.all_in_tab in ("near", "similar"):
        ids = selectable_ids(ctx.db, device_id, body.all_in_tab)
        set_selection(ctx.db, ids, body.checked)
        return SelectionOut(changed=len(ids))
    if body.all_in_tab is not None:
        return SelectionOut(
            changed=select_all_in_tab(ctx.db, device_id, body.all_in_tab, body.checked)
        )
    with ctx.db.read() as conn:  # only this phone's candidates, never a keeper
        allowed = {
            int(r[0])
            for r in conn.execute(
                "SELECT c.phone_file_id FROM candidate c "
                "JOIN phone_file p ON p.id = c.phone_file_id "
                "WHERE p.device_id = ? AND IFNULL(c.role, '') != 'keeper'",
                (device_id,),
            )
        }
    allowed |= set(selectable_ids(ctx.db, device_id, "near"))
    allowed |= set(selectable_ids(ctx.db, device_id, "similar"))
    ids = [i for i in body.ids if i in allowed]
    set_selection(ctx.db, ids, body.checked)
    return SelectionOut(changed=len(ids))
