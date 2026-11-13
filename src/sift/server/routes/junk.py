"""The Junk and Lower confidence tabs, the classifier's status, and calibration (spec 9, 12)."""

import json
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from sift.analysis.models import CLIP
from sift.backup.phonepaths import shown
from sift.cleanup import calibration
from sift.cleanup.candidates import is_checked
from sift.cleanup.junk import ensure_fresh, label_counts, state
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.cleanup import GroupOut, Item, _device
from sift.server.routes.teach import embedding_model

router = APIRouter()


def embed_version(ctx: Ctx) -> int:
    model = embedding_model(ctx)
    return model.version if model is not None else CLIP.version


def fresh(ctx: Ctx, device_id: int) -> None:
    ensure_fresh(ctx.db, device_id, ctx.config, embed_version(ctx), ctx.paths.lexicons)


def junk_groups_now(ctx: Ctx, device_id: int, tab: str) -> list[GroupOut]:
    """Each content of the tab is one group; its present phone paths are the items (E5)."""
    fresh(ctx, device_id)
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT c.group_id, p.id, p.remote_path, p.size, p.mtime, p.origin, p.content_hash, "
            "c.default_checked, s.checked, t.kind, t.width, t.height, c.junk_score, c.reasons "
            "FROM candidate c JOIN phone_file p ON p.id = c.phone_file_id "
            "JOIN content t ON t.hash = p.content_hash "
            "LEFT JOIN selection s ON s.phone_file_id = p.id "
            "WHERE c.tab = ? AND p.device_id = ? AND p.present = 1 ORDER BY c.group_id, p.id",
            (tab, device_id),
        ).fetchall()
    groups: dict[int, GroupOut] = {}
    for (
        gid,
        pid,
        path,
        size,
        mtime,
        origin,
        digest,
        default,
        chosen,
        kind,
        w,
        h,
        score,
        why,
    ) in rows:
        item = Item(
            id=pid,
            path=shown(path),
            size=size,
            mtime=mtime,
            origin=str(origin),
            kind=kind,
            width=w,
            height=h,
            checked=is_checked(default, chosen),
            role="item",
        )
        g = groups.get(gid)
        if g is None:
            g = groups[gid] = GroupOut(
                group_id=gid,
                content_hash=digest,
                keeper=None,
                copies=[],
                reclaimable=0,
                score=score,
                reasons=json.loads(why or "[]"),
            )
        g.copies.append(item)
        g.reclaimable += size
    return list(groups.values())


class JunkStatus(BaseModel):
    junk_labels: int
    keep_labels: int
    protected_labels: int
    needed_each: int
    cold_start: bool
    model_version: int
    t_suggest: float
    precheck_allowed: bool
    never_precheck: bool
    suggested: int
    maybe: int
    last_calibration: dict[str, Any] | None
    calibration_open: int  # items still to mark in the current sample


@router.get("/cleanup/junk/status")
def junk_status(ctx: Ctx) -> JunkStatus:
    device_id = _device(ctx)
    version = embed_version(ctx)
    ensure_fresh(ctx.db, device_id, ctx.config, version, ctx.paths.lexicons)
    st = state(ctx.db, ctx.config, version)
    counts = label_counts(ctx.db, version)  # the labels the model can learn from
    with ctx.db.read() as conn:
        tabs = dict(
            conn.execute(
                "SELECT c.tab, count(DISTINCT p.content_hash) FROM candidate c JOIN phone_file p "
                "ON p.id = c.phone_file_id WHERE p.device_id = ? AND p.present = 1 "
                "AND c.tab IN ('junk', 'maybe') GROUP BY c.tab",
                (device_id,),
            ).fetchall()
        )
        open_items = conn.execute(
            "SELECT count(*) FROM calibration_item WHERE verdict IS NULL"
        ).fetchone()[0]
    need = ctx.config.scoring.min_labels_each
    junk, keep = int(counts.get("junk", 0)), int(counts.get("keep", 0))
    return JunkStatus(
        junk_labels=junk,
        keep_labels=keep,
        protected_labels=int(counts.get("protected", 0)),
        needed_each=need,
        cold_start=junk < need or keep < need,
        model_version=st["model_version"],
        t_suggest=st["t_suggest"],
        precheck_allowed=st["precheck_allowed"],
        never_precheck=ctx.config.cleanup.never_precheck,
        suggested=int(tabs.get("junk", 0)),
        maybe=int(tabs.get("maybe", 0)),
        last_calibration=calibration.last(ctx.db),
        calibration_open=int(open_items),
    )


class CalItem(BaseModel):
    content_hash: str
    id: int
    path: str
    score: float
    verdict: bool | None


class CalibrationOut(BaseModel):
    items: list[CalItem]
    marked: int
    total: int
    last: dict[str, Any] | None


def _calibration(ctx: Ctx) -> CalibrationOut:
    rows = calibration.items(ctx.db)
    with ctx.db.read() as conn:
        paths = (
            dict(conn.execute("SELECT id, remote_path FROM phone_file").fetchall()) if rows else {}
        )
    items = [
        CalItem(
            content_hash=h,
            id=p,
            path=shown(str(paths.get(p, ""))),
            score=s,
            verdict=None if v is None else bool(v),
        )
        for h, p, s, v in rows
    ]
    return CalibrationOut(
        items=items,
        marked=sum(i.verdict is not None for i in items),
        total=len(items),
        last=calibration.last(ctx.db),
    )


@router.get("/cleanup/calibration")
def get_calibration(ctx: Ctx) -> CalibrationOut:
    return _calibration(ctx)


@router.post("/cleanup/calibration/start")
def start_calibration(ctx: Ctx) -> CalibrationOut:
    device_id = _device(ctx)
    try:
        calibration.start(ctx.db, device_id, ctx.config, embed_version(ctx), ctx.paths.lexicons)
    except calibration.NothingToCalibrate as e:
        raise ApiError(409, "nothing_to_calibrate", str(e)) from e
    return _calibration(ctx)


class MarkRequest(BaseModel):
    content_hash: str
    correct: bool


class MarkOut(BaseModel):
    finished: bool
    passed: bool | None = None
    errors: int | None = None
    sample_size: int | None = None
    threshold: float | None = None


@router.post("/cleanup/calibration/mark")
def mark_calibration(ctx: Ctx, body: MarkRequest) -> MarkOut:
    try:
        result = calibration.mark(
            ctx.db, ctx.config, body.content_hash, body.correct, embed_version(ctx)
        )
    except KeyError as e:
        raise ApiError(404, "not_in_sample", "This picture is not in the current sample.") from e
    if result is None:
        return MarkOut(finished=False)
    return MarkOut(
        finished=True,
        passed=result.passed,
        errors=result.errors,
        sample_size=result.sample_size,
        threshold=result.threshold,
    )
