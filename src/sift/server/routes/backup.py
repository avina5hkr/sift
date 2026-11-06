"""The backup API: start, pause, resume and stop; the run summary; failures and Retry; the
mass-disappearance guard's confirm and discard; and the backup sets.

Errors are plain words in the API's usual `{code, message, detail}` shape. A job that does not
exist is 404 and every other refusal from the job manager (no phone, a backup already running,
Sift closing) is 409. Reasons and phone paths shown here come from `sift.backup.summary`, which
shows a path in its display form only.
"""

import json
import logging
import threading
from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field, StrictBool, StrictStr, ValidationError

from sift.backup.scanner import ScanNotPending, confirm_scan, discard_scan
from sift.backup.sets import resolve_roots
from sift.backup.summary import (
    failed_files,
    latest_summary,
    reset_failed,
    undo_reset,
)
from sift.core.config import BackupSet, plain_error_message, save_config
from sift.core.jobs import TERMINAL, JobError, JobManager, JobNotFound
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.services.plainwords import describe

router = APIRouter(prefix="/backup")
_log = logging.getLogger("sift.server")
_sets_lock = threading.Lock()


def _jobs(ctx: Ctx) -> JobManager:
    if ctx.jobs is None:
        raise ApiError(409, "no_jobs", "Backups are not available right now.")
    return ctx.jobs


def _job_error(e: JobError) -> ApiError:
    return ApiError(404 if isinstance(e, JobNotFound) else 409, e.code, e.message)


def _unknown_set(e: ValueError) -> ApiError:
    text = str(e)
    return ApiError(
        422,
        "unknown_set",
        f"{text[:1].upper()}{text[1:]}. Choose sets that are in the settings.",
    )


# -- start, pause, resume, stop ------------------------------------------------------------------


class StartRequest(BaseModel):
    sets: Annotated[list[StrictStr], Field(min_length=1)]
    dry_run: StrictBool = False


class JobRef(BaseModel):
    job_id: int | None = None


class JobStarted(BaseModel):
    job_id: int


@router.post("/start")
def start_backup(ctx: Ctx, body: StartRequest) -> JobStarted:
    """Start a backup of the named sets. 409 while one is running or with no phone connected."""
    jobs = _jobs(ctx)
    try:
        resolve_roots(ctx.config.backup.sets, body.sets)
    except ValueError as e:
        raise _unknown_set(e) from e
    try:
        return JobStarted(job_id=jobs.start_backup(body.sets, dry_run=body.dry_run))
    except JobError as e:
        raise _job_error(e) from e


def _target(ctx: Ctx, body: JobRef | None) -> tuple[JobManager, int]:
    """The job a control request is about: the one named, else the one running now."""
    jobs = _jobs(ctx)
    if body is not None and body.job_id is not None:
        return jobs, body.job_id
    active = [s.id for s in jobs.snapshot() if s.state not in TERMINAL]
    if not active:
        raise ApiError(409, "no_backup_running", "No backup is running.")
    return jobs, active[-1]


def _state(jobs: JobManager, job_id: int) -> dict[str, Any]:
    return next(s.as_event() for s in jobs.snapshot() if s.id == job_id)


def _control(ctx: Ctx, body: JobRef | None, action: str) -> dict[str, Any]:
    jobs, job_id = _target(ctx, body)
    try:
        getattr(jobs, action)(job_id)
    except JobError as e:
        raise _job_error(e) from e
    return _state(jobs, job_id)


@router.post("/pause")
def pause_backup(ctx: Ctx, body: JobRef | None = None) -> dict[str, Any]:
    """Pause the running backup at the next file boundary."""
    return _control(ctx, body, "pause")


@router.post("/resume")
def resume_backup(ctx: Ctx, body: JobRef | None = None) -> dict[str, Any]:
    return _control(ctx, body, "resume")


@router.post("/stop")
def stop_backup(ctx: Ctx, body: JobRef | None = None) -> dict[str, Any]:
    """Stop at the next file boundary; the file being copied is finished first."""
    return _control(ctx, body, "stop")


# -- summary, failures, retry --------------------------------------------------------------------


class SummaryOut(BaseModel):
    job_id: int
    sets: list[str]
    outcome: str  # done | stopped | failed
    started_at: int
    finished_at: int
    duration_s: int
    new_files: int
    duplicates_skipped: int
    failed: int
    moved_links: int
    changed_files: int
    bytes: int
    warnings: list[str]
    error: str | None


@router.get("/summary")
def summary(ctx: Ctx) -> SummaryOut | None:
    """What the last finished backup did, or null before the first one."""
    found = latest_summary(ctx.db)
    return None if found is None else SummaryOut(**found)


class FailedOut(BaseModel):
    id: int
    path: str
    size: int
    set_name: str | None
    attempts: int
    reason: str


class FailuresOut(BaseModel):
    count: int
    files: list[FailedOut]


@router.get("/failures")
def failures(ctx: Ctx) -> FailuresOut:
    """Files still on the phone that could not be copied, each with a plain-words reason."""
    files = [FailedOut(**f.__dict__) for f in failed_files(ctx.db)]
    return FailuresOut(count=len(files), files=files)


class RetryOut(BaseModel):
    job_id: int
    retried: int


@router.post("/retry")
def retry_failed(ctx: Ctx) -> RetryOut:
    """Put the failed files back in the queue and start a backup (409 if one is running).

    A normal backup never retries them by itself.
    """
    jobs = _jobs(ctx)
    if any(s.state not in TERMINAL for s in jobs.snapshot()):
        raise ApiError(409, "job_conflict", "A backup is already running.")
    reset = reset_failed(ctx.db)
    if not reset:
        raise ApiError(409, "nothing_to_retry", "No files failed, so there is nothing to retry.")
    configured = {s.name.casefold(): s.name for s in ctx.config.backup.sets}
    wanted = list(
        dict.fromkeys(
            configured[r[3].casefold()] for r in reset if r[3] and r[3].casefold() in configured
        )
    )
    try:
        if not wanted:
            raise ValueError("no backup set for these files")
        job_id = jobs.start_backup(wanted)
    except JobError as e:
        undo_reset(ctx.db, reset)
        raise _job_error(e) from e
    except ValueError as e:
        undo_reset(ctx.db, reset)
        raise ApiError(
            422,
            "unknown_set",
            "The sets these files belong to are no longer in the settings.",
        ) from e
    return RetryOut(job_id=job_id, retried=len(reset))


# -- the mass-disappearance guard ----------------------------------------------------------------


class ScanOut(BaseModel):
    id: int
    state: str
    sets: list[str]
    started_at: int | None
    finished_at: int | None
    warnings: list[str]
    disappearance: list[dict[str, Any]]


def _load_scan(ctx: Ctx, scan_id: int) -> ScanOut:
    with ctx.db.read() as conn:
        r = conn.execute(
            "SELECT id, state, sets, started_at, finished_at, warnings, stats FROM scan "
            "WHERE id = ?",
            (scan_id,),
        ).fetchone()
    if r is None:
        raise ApiError(404, "scan_not_found", "That scan was not found.")
    try:
        stats = json.loads(r[6]) if r[6] else {}
    except ValueError:
        stats = {}
    return ScanOut(
        id=r[0],
        state=r[1],
        sets=json.loads(r[2]) if r[2] else [],
        started_at=r[3],
        finished_at=r[4],
        warnings=json.loads(r[5]) if r[5] else [],
        disappearance=stats.get("disappearance", []) if isinstance(stats, dict) else [],
    )


@router.get("/scan/{scan_id}")
def get_scan(ctx: Ctx, scan_id: int) -> ScanOut:
    """A scan and, when the guard stopped it, which sets or folders look emptied."""
    return _load_scan(ctx, scan_id)


def _answer_scan(ctx: Ctx, scan_id: int, action: str) -> ScanOut:
    _load_scan(ctx, scan_id)  # 404 for a scan that does not exist
    try:
        (confirm_scan if action == "confirm" else discard_scan)(ctx.db, scan_id)
    except ScanNotPending as e:
        raise ApiError(409, "scan_not_pending", describe(e)) from e
    # The waiting backup notices within a poll interval (JobControl.wait_until).
    return _load_scan(ctx, scan_id)


@router.post("/scan/{scan_id}/confirm")
def confirm(ctx: Ctx, scan_id: int) -> ScanOut:
    """Apply a scan the guard held back: the files really were deleted. The backup goes on."""
    return _answer_scan(ctx, scan_id, "confirm")


@router.post("/scan/{scan_id}/discard")
def discard(ctx: Ctx, scan_id: int) -> ScanOut:
    """Drop a held scan. Nothing changes, and the backup ends without copying."""
    return _answer_scan(ctx, scan_id, "discard")


# -- backup sets ---------------------------------------------------------------------------------


class SetsOut(BaseModel):
    sets: list[dict[str, Any]]


@router.get("/sets")
def get_sets(ctx: Ctx) -> SetsOut:
    return SetsOut(sets=[s.model_dump() for s in ctx.config.backup.sets])


def _problem(index: int, name: object, err: ValidationError) -> str:
    who = (
        f'Set {index + 1} ("{name}")'
        if isinstance(name, str) and name.strip()
        else f"Set {index + 1}"
    )
    parts = []
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"])
        parts.append(f"{where}: {plain_error_message(e)}" if where else plain_error_message(e))
    return f"{who}: " + "; ".join(parts)


@router.put("/sets")
def put_sets(ctx: Ctx, body: Annotated[dict[str, Any], Body()]) -> SetsOut:
    """Replace the backup sets. Checked with the same rules as config.toml; nothing is changed
    unless every set is valid."""
    raw = body.get("sets")
    if not isinstance(raw, list) or not raw:
        raise ApiError(422, "invalid_sets", "Send a list of at least one backup set.")
    problems: list[str] = []
    built: list[BackupSet] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"Set {i + 1}: must be an object with a name and paths.")
            continue
        try:
            built.append(BackupSet.model_validate(item))
        except ValidationError as err:
            problems.append(_problem(i, item.get("name"), err))
    if problems:
        raise ApiError(422, "invalid_sets", " ".join(problems), problems)
    with _sets_lock:
        candidate = ctx.config.backup.model_copy(deep=True)
        try:
            candidate.sets = built  # the config's own rules, set names unique ignoring case
        except ValidationError as err:
            text = "; ".join(plain_error_message(e) for e in err.errors())
            raise ApiError(422, "invalid_sets", f"{text[:1].upper()}{text[1:]}.") from err
        previous = ctx.config.backup.sets
        ctx.config.backup.sets = candidate.sets
        try:
            save_config(ctx.config, ctx.paths.config)
        except OSError as e:
            ctx.config.backup.sets = previous
            _log.exception("Could not save the backup sets")
            raise ApiError(
                500, "save_failed", "Sift could not save the settings. Nothing was changed."
            ) from e
    return SetsOut(sets=[s.model_dump() for s in ctx.config.backup.sets])
