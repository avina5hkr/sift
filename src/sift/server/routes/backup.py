"""The backup API: start, pause, resume and stop; the run summary; failures and Retry; the
mass-disappearance guard's confirm and discard; and the backup sets.

Errors are plain words in the API's usual `{code, message, detail}` shape. A job that does not
exist is 404 and every other refusal from the job manager (no phone, a backup already running,
Sift closing) is 409. Reasons and phone paths shown here come from `sift.backup.summary`, which
shows a path in its display form only.
"""

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field, StrictBool, StrictStr, ValidationError

from sift.backup.phonepaths import from_db, shown
from sift.backup.scanner import ScanNotPending, confirm_scan, discard_scan
from sift.backup.sets import resolve_roots
from sift.backup.summary import (
    failed_files,
    latest_summary,
    reset_failed,
    undo_reset,
)
from sift.core.config import BackupSet, plain_error_message, save_config
from sift.core.jobs import NO_PHONE_MESSAGE, TERMINAL, JobError, JobManager, JobNotFound, NoPhone
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.services.plainwords import describe

router = APIRouter(prefix="/backup")
_log = logging.getLogger("sift.server")
_sets_lock = threading.Lock()
_run_lock = threading.Lock()  # Start and Retry: check, change and start as one step


def _connected_device_id(ctx: Ctx) -> int | None:
    """The stored id of the phone that is connected now, or None (no phone, no tracker)."""
    tracker = ctx.tracker
    session = tracker.session() if tracker is not None else None
    if session is None:
        return None
    with ctx.db.read() as conn:
        row = conn.execute(
            "SELECT id FROM device WHERE uuid = ?", (session.device_uuid,)
        ).fetchone()
    return int(row[0]) if row is not None else None


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
        with _run_lock:
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
    """What the connected phone's last finished backup did; null before its first one, or with
    no phone connected."""
    device_id = _connected_device_id(ctx)
    found = None if device_id is None else latest_summary(ctx.db, device_id=device_id)
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
    """Files still on the connected phone that could not be copied, each with a plain-words
    reason. Empty with no phone connected."""
    device_id = _connected_device_id(ctx)
    found = [] if device_id is None else failed_files(ctx.db, device_id)
    files = [FailedOut(**f.__dict__) for f in found]
    return FailuresOut(count=len(files), files=files)


class RetryOut(BaseModel):
    job_id: int
    retried: int


@router.post("/retry")
def retry_failed(ctx: Ctx) -> RetryOut:
    """Put the failed files back in the queue and start a backup (409 if one is running).

    A normal backup never retries them by itself. Only the connected phone's files of sets that
    are still in the settings are queued; the rest stay listed as failed. With no phone
    connected it is a plain 409 and nothing changes. The whole sequence (is one running,
    reset, start) runs under one lock, shared with Start, so two requests cannot interleave.
    """
    jobs = _jobs(ctx)
    with _run_lock:
        if any(s.state not in TERMINAL for s in jobs.snapshot()):
            raise ApiError(409, "job_conflict", "A backup is already running.")
        device_id = _connected_device_id(ctx)
        if device_id is None:
            raise ApiError(409, NoPhone.code, NO_PHONE_MESSAGE)
        configured = {s.name.casefold(): s.name for s in ctx.config.backup.sets}
        reset = reset_failed(ctx.db, device_id, configured)
        if not reset:
            if failed_files(ctx.db, device_id):
                raise ApiError(
                    422,
                    "unknown_set",
                    "The sets these files belong to are no longer in the settings.",
                )
            raise ApiError(
                409, "nothing_to_retry", "No files failed, so there is nothing to retry."
            )
        wanted = list(dict.fromkeys(configured[r[3].casefold()] for r in reset if r[3]))
        try:
            job_id = jobs.start_backup(wanted)
        except JobError as e:
            undo_reset(ctx.db, reset)
            raise _job_error(e) from e
        except BaseException:
            undo_reset(ctx.db, reset)  # whatever went wrong, the files stay listed as failed
            raise
    return RetryOut(job_id=job_id, retried=len(reset))


# -- the mass-disappearance guard ----------------------------------------------------------------


class ScanOut(BaseModel):
    id: int
    device_id: int | None
    state: str
    sets: list[str]
    started_at: int | None
    finished_at: int | None
    warnings: list[str]
    disappearance: list[dict[str, Any]]


def _load_scan(ctx: Ctx, scan_id: int) -> ScanOut:
    with ctx.db.read() as conn:
        r = conn.execute(
            "SELECT id, state, sets, started_at, finished_at, warnings, stats, device_id "
            "FROM scan WHERE id = ?",
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
        device_id=r[7],
        state=r[1],
        sets=json.loads(r[2]) if r[2] else [],
        started_at=r[3],
        finished_at=r[4],
        warnings=[shown(str(w)) for w in json.loads(r[5])] if r[5] else [],
        disappearance=stats.get("disappearance", []) if isinstance(stats, dict) else [],
    )


@router.get("/scan/{scan_id}")
def get_scan(ctx: Ctx, scan_id: int) -> ScanOut:
    """A scan and, when the guard stopped it, which sets or folders look emptied."""
    return _load_scan(ctx, scan_id)


def _answer_scan(ctx: Ctx, scan_id: int, action: str) -> ScanOut:
    scan = _load_scan(ctx, scan_id)  # 404 for a scan that does not exist
    connected = _connected_device_id(ctx)
    if scan.device_id is not None and scan.device_id != connected:
        raise ApiError(
            409,
            "wrong_phone",
            "That scan belongs to a different phone. "
            "Connect the phone it was made on to answer it.",
        )
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


# -- what the Backup screen shows besides the running job ------------------------------------------


class SetStatus(BaseModel):
    name: str
    enabled: bool
    on_phone: int
    backed_up: int
    pending: int


class RecentFile(BaseModel):
    path: str
    size: int
    at: int


class OverviewOut(BaseModel):
    phone_connected: bool
    sets: list[SetStatus]
    recent: list[RecentFile]
    pending_scan: ScanOut | None
    job: dict[str, Any] | None  # the backup running now, as in a `progress` event, else null


RECENT_FILES = 8


@router.get("/overview")
def overview(ctx: Ctx) -> OverviewOut:
    """What the screen shows besides the running job, for the phone that is connected now:
    files per set (on the phone, backed up, still to copy), the last few files copied and the
    scan the guard is holding for an answer. With no phone connected the counts are empty and
    `phone_connected` is false. Also the backup running now (so a window opened mid-run knows
    where it is before the next `progress` event)."""
    device_id = _connected_device_id(ctx)
    counts: dict[str, tuple[int, int, int]] = {}
    recent: list[Any] = []
    held = None
    if device_id is not None:
        with ctx.db.read() as conn:
            counts = {
                str(r[0]).casefold(): (int(r[1]), int(r[2]), int(r[3]))
                for r in conn.execute(
                    "SELECT set_name, COUNT(*), SUM(state = 'done'), "
                    "SUM(state IN ('discovered', 'pulling', 'failed')) FROM phone_file "
                    "WHERE device_id = ? AND present = 1 AND state NOT IN ('skipped', 'deleted') "
                    "AND set_name IS NOT NULL GROUP BY set_name",
                    (device_id,),
                )
            }
            recent = conn.execute(
                "SELECT f.remote_path, f.size, MAX(r.verified_at) AS at FROM phone_file f "
                "JOIN replica r ON r.content_hash = f.content_hash "
                "WHERE f.device_id = ? AND f.state = 'done' "
                "GROUP BY f.id ORDER BY at DESC, f.id DESC LIMIT ?",
                (device_id, RECENT_FILES),
            ).fetchall()
            held = conn.execute(
                "SELECT id FROM scan WHERE state = 'awaiting_confirmation' AND device_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
    sets = []
    for s in ctx.config.backup.sets:
        on_phone, backed_up, pending = counts.get(s.name.casefold(), (0, 0, 0))
        sets.append(
            SetStatus(
                name=s.name,
                enabled=s.enabled,
                on_phone=on_phone,
                backed_up=backed_up,
                pending=pending,
            )
        )
    running = (
        [s for s in ctx.jobs.snapshot() if s.state not in TERMINAL and s.type == "backup"]
        if ctx.jobs is not None
        else []
    )
    return OverviewOut(
        phone_connected=device_id is not None,
        job=running[-1].as_event() if running else None,
        sets=sets,
        recent=[
            RecentFile(path=shown(from_db(r[0])), size=int(r[1]), at=int(r[2])) for r in recent
        ],
        pending_scan=_load_scan(ctx, int(held[0])) if held else None,
    )


# -- opening the backup folder ---------------------------------------------------------------------


@router.post("/open-folder")
def open_folder(ctx: Ctx) -> dict[str, bool]:
    """Show the configured Mac backup folder in Finder. It opens that folder and nothing else."""
    root = Path(ctx.config.backup.mac_root).expanduser()
    if not root.is_absolute():
        raise ApiError(
            409, "no_backup_folder", "The backup folder is not a full path. Check it in Settings."
        )
    if root.suffix.casefold() == ".app":
        # `open` would start an app rather than show a folder.
        raise ApiError(
            409, "no_backup_folder", "Sift will not open this folder because its name ends in .app."
        )
    if not root.is_dir():
        raise ApiError(
            409, "no_backup_folder", "The backup folder does not exist yet. Run a backup first."
        )
    try:
        subprocess.run(["open", "--", str(root)], check=True, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        _log.warning("Could not open the backup folder: %s", e)
        raise ApiError(
            500, "open_failed", "Sift could not open the backup folder. Open it from Finder."
        ) from e
    return {"opened": True}


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
