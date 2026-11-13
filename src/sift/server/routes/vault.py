"""Verify, Problems and Repair, and the long-task status (Verify, Adopt, Rebuild)."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from sift.backup.problems import open_problems
from sift.backup.replicator import Dest, load_dest
from sift.backup.verifier import NothingToRepairFrom, repair, verify_dest
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.services.tasks import TaskBusy, TaskControl, TaskRunner, TaskStatus

router = APIRouter()


class TaskOut(BaseModel):
    kind: str
    state: str
    done: int
    total: int
    message: str | None
    result: dict[str, Any]


def _out(status: TaskStatus) -> TaskOut:
    return TaskOut(**status.as_event())


def tasks_of(ctx: Ctx) -> TaskRunner:
    if ctx.tasks is None:
        raise ApiError(503, "not_ready", "Sift is still starting. Try again in a moment.")
    return ctx.tasks


def start_task(ctx: Ctx, kind: str, work: Any) -> TaskOut:
    try:
        return _out(tasks_of(ctx).start(kind, work))
    except TaskBusy as e:
        raise ApiError(409, "task_running", str(e)) from e


def mounted_dests(ctx: Ctx) -> list[Dest]:
    with ctx.db.read() as conn:
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM destination ORDER BY id")]
    found = [load_dest(ctx.db, i) for i in ids]
    return [d for d in found if d is not None and d.root.is_dir()]


@router.post("/backup/verify")
def start_verify(ctx: Ctx) -> TaskOut:
    """Re-hash every backed-up copy on every plugged-in destination."""

    def work(control: TaskControl) -> dict[str, Any]:
        totals = {"checked": 0, "ok": 0, "mismatched": 0, "missing": 0}
        for dest in mounted_dests(ctx):
            stats = verify_dest(
                ctx.db, dest, should_stop=lambda: control.should_stop, on_file=control.progress
            )
            for key in totals:
                totals[key] += getattr(stats, key)
        return totals

    return start_task(ctx, "verify", work)


@router.get("/tasks/current")
def current_task(ctx: Ctx) -> TaskOut | None:
    status = tasks_of(ctx).current()
    return None if status is None else _out(status)


@router.post("/tasks/stop")
def stop_task(ctx: Ctx) -> TaskOut | None:
    runner = tasks_of(ctx)
    runner.stop(timeout_s=0.0)
    status = runner.current()
    return None if status is None else _out(status)


class ProblemOut(BaseModel):
    id: int
    kind: str
    destination: str | None
    rel_path: str | None
    detail: str | None
    created_at: int
    repairable: bool


@router.get("/backup/problems")
def problems(ctx: Ctx) -> list[ProblemOut]:
    return [
        ProblemOut(
            id=p.id,
            kind=p.kind,
            destination=p.dest_kind,
            rel_path=p.rel_path,
            detail=p.detail,
            created_at=p.created_at,
            repairable=p.repairable,
        )
        for p in open_problems(ctx.db)
    ]


class RepairOut(BaseModel):
    rel_path: str


@router.post("/backup/problems/{problem_id}/repair")
def repair_problem(ctx: Ctx, problem_id: int) -> RepairOut:
    try:
        return RepairOut(rel_path=repair(ctx.db, problem_id))
    except LookupError as e:
        raise ApiError(404, "not_found", str(e)) from e
    except NothingToRepairFrom as e:
        raise ApiError(409, "no_good_copy", str(e)) from e
