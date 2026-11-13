"""Verify, Problems and Repair, and the long-task status (Verify, Adopt, Rebuild)."""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from sift.backup.adopt import adopt
from sift.backup.confirm import build_report, confirm, latest
from sift.backup.problems import open_problems
from sift.backup.replicator import Dest
from sift.backup.run import usable_dests
from sift.backup.verifier import NothingToRepairFrom, repair, verify_dest
from sift.cleanup.analyse import load_analysis, run_analysis
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
    """The current Mac folder and the SSD that is plugged in (by UUID), never a look-alike."""
    return usable_dests(ctx)


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
        usable = {d.id for d in usable_dests(ctx)}
        return RepairOut(rel_path=repair(ctx.db, problem_id, usable=usable))
    except LookupError as e:
        raise ApiError(404, "not_found", str(e)) from e
    except NothingToRepairFrom as e:
        raise ApiError(409, "no_good_copy", str(e)) from e


class AdoptRequest(BaseModel):
    destination: str  # 'mac' or 'ssd'


@router.post("/backup/adopt")
def start_adopt(ctx: Ctx, body: AdoptRequest) -> TaskOut:
    """Index the files already in a backup folder, in place. Nothing in it is changed."""
    dests = [d for d in mounted_dests(ctx) if d.kind == body.destination]
    if not dests:
        raise ApiError(
            409, "not_mounted", f"The {body.destination.upper()} backup folder is not available."
        )
    dest = dests[0]

    def work(control: TaskControl) -> dict[str, Any]:
        stats = adopt(
            ctx.db,
            dest,
            mode=ctx.config.backup.adopt_mode,
            should_stop=lambda: control.should_stop,
            on_file=control.progress,
        )
        return asdict(stats)

    return start_task(ctx, "adopt", work)


@router.post("/backup/rebuild")
def start_rebuild(ctx: Ctx) -> TaskOut:
    """After a lost database: index every plugged-in backup folder again."""

    def work(control: TaskControl) -> dict[str, Any]:
        totals: dict[str, int] = {}
        for dest in mounted_dests(ctx):
            stats = adopt(
                ctx.db, dest, should_stop=lambda: control.should_stop, on_file=control.progress
            )
            for key, value in asdict(stats).items():
                totals[key] = totals.get(key, 0) + int(value)
        return totals

    return start_task(ctx, "rebuild", work)


class ConfirmationOut(BaseModel):
    set_name: str
    report: dict[str, Any]  # the live checks (no phone: phone_unchecked counts what is pending)
    last_confirmed_at: int | None
    last_confirmed: bool
    stale_reason: str | None


def _phone(ctx: Ctx) -> tuple[int, Any]:
    from sift.server.routes.backup import _connected_device_id

    device_id = _connected_device_id(ctx)
    session = ctx.tracker.session() if ctx.tracker is not None else None
    if device_id is None or session is None:
        raise ApiError(409, "no_phone", "Connect the phone you back up, then try again.")
    return device_id, session


@router.get("/backup/confirmation")
def confirmation(ctx: Ctx, set: str) -> ConfirmationOut:
    from sift.server.routes.backup import _connected_device_id

    device_id = _connected_device_id(ctx)
    if device_id is None:
        raise ApiError(409, "no_phone", "Connect the phone you back up, then try again.")
    live = build_report(ctx.db, ctx.config, device_id, set)
    last = latest(ctx.db, ctx.config, device_id, set)
    return ConfirmationOut(
        set_name=set,
        report=asdict(live),
        last_confirmed_at=last.created_at if last else None,
        last_confirmed=bool(last and last.confirmed),
        stale_reason=last.stale_reason if last else None,
    )


class ConfirmRequest(BaseModel):
    set: str


@router.post("/backup/confirm")
def start_confirm(ctx: Ctx, body: ConfirmRequest) -> TaskOut:
    """Check the set with the phone's own hashes and save a dated confirmation."""
    if body.set not in {s.name for s in ctx.config.backup.sets}:
        raise ApiError(404, "unknown_set", f"There is no set called {body.set!r}.")
    device_id, session = _phone(ctx)

    def work(control: TaskControl) -> dict[str, Any]:
        done = confirm(
            ctx.db,
            ctx.config,
            device_id,
            body.set,
            session.transport,
            dests=usable_dests(ctx),
            should_stop=lambda: control.should_stop,
        )
        return {"confirmed": done.confirmed, "files": done.files, "report": done.report}

    return start_task(ctx, "confirm", work)


class AnalyseRequest(BaseModel):
    sets: list[str]


@router.post("/analyse/phone")
def start_analyse(ctx: Ctx, body: AnalyseRequest) -> TaskOut:
    """Read-only: how much of each set is already backed up and verified."""
    known = {s.name for s in ctx.config.backup.sets}
    unknown = [s for s in body.sets if s not in known]
    if not body.sets or unknown:
        raise ApiError(400, "bad_sets", "Choose one or more of your backup sets to analyse.")
    device_id, session = _phone(ctx)

    def work(control: TaskControl) -> dict[str, Any]:
        run_id = run_analysis(
            ctx.db,
            ctx.config,
            session.transport,
            device_id,
            body.sets,
            should_stop=lambda: control.should_stop,
            on_progress=control.progress,
        )
        return {"analysis_id": run_id}

    return start_task(ctx, "analyse", work)


@router.get("/analyse/phone/{run_id}")
def get_analysis(ctx: Ctx, run_id: int) -> dict[str, Any]:
    found = load_analysis(ctx.db, run_id)
    if found is None:
        raise ApiError(404, "not_found", "That analysis does not exist.")
    return found
