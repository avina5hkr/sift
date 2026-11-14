"""Deleting from the phone (spec 10, 10E, 12): preflight, execute, quarantine, Ready to free.

Nothing here deletes without an explicit POST /delete/execute naming a ready run (I7). Both the
preflight and the execute run as TaskRunner tasks, so they are cancellable and only one runs.
"""

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sift.backup.confirm import latest
from sift.cleanup import quarantine
from sift.cleanup.deleter import check_execute, execute
from sift.cleanup.errors import ExecuteRefused, ManifestBlocked, RestoreRefused
from sift.cleanup.preflight import create_run, ids_for_sets, run_preflight, summary
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.cleanup import _device
from sift.server.routes.vault import TaskOut, start_task
from sift.services.tasks import TaskControl

router = APIRouter()
_ACTIVE = ("done", "failed", "stopped")


def _session(ctx: Ctx) -> Any:
    return ctx.tracker.session() if ctx.tracker is not None else None


def _no_backup_running(ctx: Ctx) -> None:
    jobs = ctx.jobs.snapshot() if ctx.jobs is not None else []
    if any(str(j.state) not in _ACTIVE for j in jobs):
        raise ApiError(409, "job_conflict", "A backup is running. Let it finish first.")


class PreflightRequest(BaseModel):
    source: Literal["cleanup", "analysis"]
    ids: list[int] = Field(default_factory=list, max_length=50_000)  # cleanup: the ticked files
    sets: list[str] = Field(default_factory=list)  # analysis: the sets to free


class PreflightStarted(BaseModel):
    run_id: int
    task: TaskOut


@router.post("/delete/preflight")
def start_preflight(ctx: Ctx, body: PreflightRequest) -> PreflightStarted:
    device_id = _device(ctx)
    _no_backup_running(ctx)
    if body.source == "analysis":
        ids = ids_for_sets(ctx, device_id, body.sets)
        disposition = "archive"
    else:
        with ctx.db.read() as conn:  # only files the owner ticked count
            ticked = {
                int(r[0])
                for r in conn.execute("SELECT phone_file_id FROM selection WHERE checked = 1")
            }
        ids = [i for i in body.ids if i in ticked]
        disposition = "quarantine"
    if not ids:
        raise ApiError(409, "nothing_selected", "There is nothing to delete.")
    run_id = create_run(ctx, device_id, body.source, ids, disposition)

    def work(control: TaskControl) -> dict[str, Any]:
        return run_preflight(
            ctx,
            run_id,
            _session(ctx),
            should_stop=lambda: control.should_stop,
            on_progress=control.progress,
        )

    return PreflightStarted(run_id=run_id, task=start_task(ctx, "preflight", work))


@router.get("/delete/runs/{run_id}")
def get_run(ctx: Ctx, run_id: int) -> dict[str, Any]:
    found = summary(ctx, run_id)
    if found is None:
        raise ApiError(404, "unknown_run", "There is no such delete run.")
    found["backups"] = [d.root.as_posix() for d in _dests(ctx)]
    return found


def _dests(ctx: Ctx) -> list[Any]:
    from sift.backup.run import usable_dests

    return usable_dests(ctx)


class ExecuteRequest(BaseModel):
    run_id: int
    confirm: bool = False
    typed_count: int | None = None


@router.post("/delete/execute")
def start_execute(ctx: Ctx, body: ExecuteRequest) -> TaskOut:
    _no_backup_running(ctx)
    session = _session(ctx)
    try:
        check_execute(ctx, body.run_id, session, confirm=body.confirm, typed_count=body.typed_count)
    except ExecuteRefused as e:
        raise ApiError(409, e.code, str(e)) from e

    def work(control: TaskControl) -> dict[str, Any]:
        try:
            return execute(
                ctx,
                body.run_id,
                session,
                should_stop=lambda: control.should_stop,
                on_progress=control.progress,
            )
        except ManifestBlocked as e:
            with ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE delete_run SET state = 'blocked', blocked_reason = ? WHERE id = ?",
                    (str(e), body.run_id),
                )
            raise

    return start_task(ctx, "delete", work)


class QuarantineItem(BaseModel):
    id: int
    content_hash: str
    phone_path: str | None
    size: int
    moved_at: int
    purge_after: int


@router.get("/quarantine")
def list_quarantine(ctx: Ctx) -> list[QuarantineItem]:
    return [
        QuarantineItem(
            id=e.id,
            content_hash=e.content_hash,
            phone_path=e.phone_path,
            size=e.size,
            moved_at=e.moved_at,
            purge_after=e.purge_after,
        )
        for e in quarantine.entries(ctx)
    ]


class RestoreRequest(BaseModel):
    ids: list[int] = Field(min_length=1)
    to: Literal["phone", "backup"]


@router.post("/quarantine/restore")
def restore(ctx: Ctx, body: RestoreRequest) -> dict[str, int]:
    _no_backup_running(ctx)
    try:
        return quarantine.restore(ctx, body.ids, body.to, _session(ctx))
    except RestoreRefused as e:
        raise ApiError(409, "restore_refused", str(e)) from e


@router.post("/quarantine/purge")
def purge(ctx: Ctx) -> dict[str, int]:
    return {"purged": quarantine.purge_due(ctx)}


class ReadySet(BaseModel):
    set_name: str
    files: int
    bytes: int
    confirmed_at: int


@router.get("/cleanup/ready")
def ready_to_free(ctx: Ctx) -> list[ReadySet]:
    """Confirmed, still-valid sets that allow Delete later (spec 10E "Ready to free")."""
    device_id = _device(ctx)
    out = []
    for s in ctx.config.backup.sets:
        if not s.enabled or not s.deletable:
            continue
        c = latest(ctx.db, ctx.config, device_id, s.name)
        if c is not None and c.confirmed and c.stale_reason is None and c.files:
            out.append(
                ReadySet(set_name=s.name, files=c.files, bytes=c.bytes, confirmed_at=c.created_at)
            )
    return out
