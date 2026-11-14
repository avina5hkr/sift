"""Clean slate (spec 10A, 12): plan from keep rules, tree, adjust, backup gaps, run."""

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sift.cleanup import slate
from sift.cleanup.preflight import create_run, run_preflight
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.cleanup import _device
from sift.server.routes.delete import (
    PreflightStarted,
    _no_backup_running,
    _session,
    _usb_for_delete,
)
from sift.server.routes.vault import start_task
from sift.services.tasks import TaskControl

router = APIRouter()
FILES_PAGE = 500


class RulesIn(BaseModel):
    keep_folders: list[str] | None = None
    keep_newer_than_days: int | None = Field(default=None, ge=0, le=36500)
    keep_labelled: bool = True
    keep_camera: bool = False
    patterns: list[str] = Field(default_factory=list, max_length=100)


def _clean(paths: list[str], *, pattern: bool) -> list[str]:
    """Normalise keep folders or patterns; one that cannot name a phone path is refused, never
    dropped (dropping it would plan its files for removal)."""
    out = []
    for raw in paths:
        if not raw.strip():
            continue
        found = slate.normalise(raw, pattern=pattern)
        if found is None:
            raise ApiError(
                422, "bad_keep_path", f"Sift cannot use {raw!r}: keep paths start with /sdcard/."
            )
        out.append(found)
    return out


def _active_run(ctx: Ctx, plan_id: int) -> tuple[int, str] | None:
    with ctx.db.read() as conn:
        row = conn.execute(
            "SELECT r.id, r.state FROM slate_plan p JOIN delete_run r ON r.id = p.run_id "
            "WHERE p.id = ? AND r.state IN ('preflight', 'ready', 'running')",
            (plan_id,),
        ).fetchone()
    return None if row is None else (int(row[0]), str(row[1]))


def _items(ctx: Ctx, plan_id: int) -> list[slate.Planned]:
    try:
        return slate.plan(ctx, plan_id)
    except KeyError as e:
        raise ApiError(404, "unknown_plan", "There is no such Clean slate plan.") from e


def _out(ctx: Ctx, plan_id: int) -> dict[str, Any]:
    _device_id, rules, kept_ids, kept_folders = slate.load_plan(ctx, plan_id)
    items = _items(ctx, plan_id)
    return {
        "id": plan_id,
        "rules": asdict(rules),
        "kept_by_hand": {"files": len(kept_ids), "folders": kept_folders},
        **slate.summary(items),
        "gap_sets": slate.gap_sets(ctx, items),
        "fingerprint": slate.fingerprint(items),
    }


@router.post("/slate/plan")
def create_plan(ctx: Ctx, body: RulesIn) -> dict[str, Any]:
    device_id = _device(ctx)
    rules = slate.Rules.defaults(ctx.config)
    if body.keep_folders is not None:
        rules.keep_folders = _clean(body.keep_folders, pattern=False)
    if body.keep_newer_than_days is not None:
        rules.keep_newer_than_days = body.keep_newer_than_days
    rules.keep_labelled, rules.keep_camera = body.keep_labelled, body.keep_camera
    rules.patterns = _clean(body.patterns, pattern=True)
    return _out(ctx, slate.create(ctx, device_id, rules))


@router.get("/slate/plan/{plan_id}")
def get_plan(ctx: Ctx, plan_id: int) -> dict[str, Any]:
    _items(ctx, plan_id)
    return _out(ctx, plan_id)


@router.get("/slate/plan/{plan_id}/tree")
def plan_tree(
    ctx: Ctx, plan_id: int, side: Literal["remove", "keep", "protected"] = "remove"
) -> list[dict[str, Any]]:
    return slate.tree(_items(ctx, plan_id), side)


@router.get("/slate/plan/{plan_id}/files")
def plan_files(
    ctx: Ctx,
    plan_id: int,
    folder: str = "",
    side: Literal["remove", "keep", "protected", "gaps"] = "remove",
    offset: int = 0,
) -> dict[str, Any]:
    items = _items(ctx, plan_id)
    if side == "gaps":
        picked = [p for p in items if p.gap is not None]
    elif side == "remove":
        picked = slate.removal(items)
    else:
        picked = [p for p in items if p.decision.action == side]
    if folder:
        base = folder.rstrip("/") + "/"
        picked = [p for p in picked if p.row.path.startswith(base)]
    picked.sort(key=lambda p: -p.row.size)
    return {
        "total_files": len(picked),
        "files": [
            {"id": p.row.id, "path": p.row.path, "size": p.row.size, "mtime": p.row.mtime,
             "kind": p.row.kind, "status": p.row.status, "reason": p.decision.reason,
             "category": p.category, "gap": p.gap}
            for p in picked[offset : offset + FILES_PAGE]
        ],
    }  # fmt: skip


class KeepIn(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=50_000)
    folders: list[str] = Field(default_factory=list, max_length=500)


@router.post("/slate/plan/{plan_id}/keep")
def keep(ctx: Ctx, plan_id: int, body: KeepIn) -> dict[str, Any]:
    _items(ctx, plan_id)
    active = _active_run(ctx, plan_id)
    if active is not None and active[1] in ("preflight", "running"):
        raise ApiError(409, "run_active", "Wait until the current run ends, then change the plan.")
    folders = _clean(body.folders, pattern=False)
    slate.keep(ctx, plan_id, body.ids, folders)
    if active is not None:  # checks already passed: they no longer match the plan
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE delete_run SET state = 'blocked', blocked_reason = ? WHERE id = ?",
                ("The plan changed. Check the files again.", active[0]),
            )
    return _out(ctx, plan_id)


@router.post("/slate/plan/{plan_id}/backup-gaps")
def backup_gaps(ctx: Ctx, plan_id: int) -> dict[str, Any]:
    sets = slate.gap_sets(ctx, _items(ctx, plan_id))
    if not sets:
        raise ApiError(409, "no_gaps", "Every file to remove is already backed up.")
    if ctx.jobs is None:
        raise ApiError(409, "no_jobs", "Backups are not available right now.")
    from sift.core.jobs import JobConflict, NoPhone

    try:
        job_id = ctx.jobs.start_backup(sets)
    except (JobConflict, NoPhone) as e:
        raise ApiError(409, "job_conflict", str(e)) from e
    return {"job_id": job_id, "sets": sets}


class SlateRun(BaseModel):
    dispositions: dict[str, Literal["archive", "quarantine"]] = Field(default_factory=dict)
    fingerprint: str | None = None  # the plan the owner reviewed


@router.post("/slate/plan/{plan_id}/preflight")
def start_checks(ctx: Ctx, plan_id: int, body: SlateRun) -> PreflightStarted:
    """The plan's removal set, checked like every delete (spec 10A step 5)."""
    _no_backup_running(ctx)
    _usb_for_delete(ctx)
    device_id = _device(ctx)
    items = _items(ctx, plan_id)
    if any(p.gap is not None for p in items):
        raise ApiError(
            409, "gaps", "Some files to remove are not backed up yet. Back them up or keep them."
        )
    if body.fingerprint is not None and body.fingerprint != slate.fingerprint(items):
        raise ApiError(
            409, "plan_changed", "The plan changed since you reviewed it. Look at it again."
        )
    chosen = slate.removal(items)
    if not chosen:
        raise ApiError(409, "nothing_selected", "There is nothing to remove.")
    default = ctx.config.slate.disposition_default
    quarantined = {
        p.row.id for p in chosen if body.dispositions.get(p.category, default) == "quarantine"
    }
    run_id = create_run(
        ctx, device_id, "slate", [p.row.id for p in chosen],
        "quarantine" if quarantined else "archive",
    )  # fmt: skip
    with ctx.db.tx() as conn:
        conn.execute("UPDATE slate_plan SET run_id = ? WHERE id = ?", (run_id, plan_id))
        conn.executemany(
            "UPDATE delete_item SET quarantine = 1 WHERE run_id = ? AND phone_file_id = ?",
            [(run_id, pid) for pid in quarantined],
        )

    def work(control: TaskControl) -> dict[str, Any]:
        return run_preflight(
            ctx, run_id, _session(ctx), should_stop=lambda: control.should_stop,
            on_progress=control.progress,
        )  # fmt: skip

    return PreflightStarted(run_id=run_id, task=start_task(ctx, "preflight", work))
