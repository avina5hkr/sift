"""One backup run, as the job manager's runner: scan, the guard, then pull.

`run_backup` is what the app's `JobManager` runs for each backup job:

1. **Scan** the ticked sets (redone from the start if the phone disconnects or stops
   responding meanwhile).
2. **Guard:** when the mass-disappearance guard stops the scan, the job shows
   `awaiting_confirmation` and waits (stop ends the wait) until the owner confirms the scan,
   which applies it, or discards it, which ends the run with nothing copied.
3. A dry run stops here, with the totals the scan found.
4. **Space:** the bytes still to copy, plus the headroom, must be free on the Mac.
5. **Pull** what is still to copy in the ticked sets (`sift.backup.puller`).
"""

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sift.backup.destination import sanitize_label
from sift.backup.puller import RECONNECT_PAUSE_S, Puller, PullStats, Target, pending_items
from sift.backup.replicator import REPLICATION_LOCK, Dest, Replicator, load_dest
from sift.backup.scanner import ScanResult, scan, space_check
from sift.backup.sets import resolve_roots
from sift.backup.skiprules import SkipRules
from sift.backup.summary import record_summary
from sift.core.clock import Clock, SystemClock
from sift.core.config import Config
from sift.core.context import AppContext
from sift.core.db import Database
from sift.core.jobs import JobControl, JobRefused, JobState, JobStopped
from sift.device.session import DeviceSession
from sift.device.transport import DeviceDisconnected, DeviceTimeout
from sift.services.plainwords import describe

log = logging.getLogger(__name__)

UNKNOWN_SET_MESSAGE = "A backup set chosen for this backup is no longer in the settings."
MAC_LABEL = "Mac"


def device_label(db: Database, config: Config, device_uuid: str | None) -> str:
    """The folder name of a phone under the backup folder: its own label, else the default one.

    Each phone has its own label (`device.label`, set when it is chosen), so two phones never
    share a folder. A phone with no label of its own (stored before labels existed) uses
    `backup.device_label`. Either is made safe (`sanitize_label`) whatever was stored, so a
    name typed into config.toml can never be `.staging` or hold a `/`.
    """
    if device_uuid is not None:
        with db.read() as conn:
            row = conn.execute("SELECT label FROM device WHERE uuid = ?", (device_uuid,)).fetchone()
        if row is not None and row[0]:
            return sanitize_label(str(row[0]))
    return sanitize_label(config.backup.device_label)


def mac_target(db: Database, config: Config, device_uuid: str | None = None) -> Target:
    """The Mac destination for `backup.mac_root` (its row created on first use), with the folder
    name of the phone `device_uuid`."""
    root = Path(config.backup.mac_root).expanduser()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id FROM destination WHERE kind = 'mac' AND root_path = ?", (str(root),)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO destination (kind, root_path, label, required) "
                "VALUES ('mac', ?, ?, 1)",
                (str(root), MAC_LABEL),
            )
            dest_id = int(cur.lastrowid or 0)
        else:
            dest_id = int(row[0])
    return Target(dest_id, root, device_label(db, config, device_uuid), config.backup.layout)


def reader_count(config: Config, session: DeviceSession) -> int:
    """`phone.parallel_reads_usb`, leaving one of the phone's connection slots for the rest."""
    wanted = config.phone.parallel_reads_usb
    most = getattr(getattr(session.transport, "limits", None), "max_connections", None)
    return wanted if most is None else max(1, min(wanted, most - 1))


def _scan_state(db: Database, scan_id: int) -> str:
    with db.read() as conn:
        row = conn.execute("SELECT state FROM scan WHERE id = ?", (scan_id,)).fetchone()
    return str(row[0]) if row is not None else "discarded"


def _stored_device_id(db: Database, device_uuid: str) -> int | None:
    with db.read() as conn:
        row = conn.execute("SELECT id FROM device WHERE uuid = ?", (device_uuid,)).fetchone()
    return None if row is None else int(row[0])


def _device_id(db: Database, device_uuid: str) -> int:
    found = _stored_device_id(db, device_uuid)
    if found is None:
        raise ValueError("the phone being backed up is not stored")
    return found


@dataclass
class _Progress:
    """What a run knows so far, for the summary row it leaves however it ends."""

    stats: PullStats = field(default_factory=PullStats)
    device_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    discarded: bool = False  # the owner discarded the scan the guard held: nothing was copied


def ssd_dest(ctx: AppContext) -> Dest | None:
    """The SSD destination when the SSD is plugged in, else None."""
    state = ctx.ssd.state() if ctx.ssd is not None else None
    if state is None or not state.connected or state.dest_id is None:
        return None
    return load_dest(ctx.db, state.dest_id)


def usable_dests(ctx: AppContext) -> list[Dest]:
    """The destinations to work on now: the Mac folder of `backup.mac_root` when it exists, and
    the configured SSD only when it is plugged in (found by its volume UUID, never by path)."""
    found: list[Dest] = []
    if ctx.config.backup.mac_root:
        target = mac_target(ctx.db, ctx.config)
        if target.root.is_dir():
            found.append(Dest(target.dest_id, "mac", target.root))
    ssd = ssd_dest(ctx)
    if ssd is not None and ssd.root.is_dir():
        found.append(ssd)
    return found


def ssd_pending(ctx: AppContext, mac: Dest) -> int:
    """Files on the Mac with no copy on the configured SSD (all of them if it was never seen)."""
    uuid = ctx.config.backup.ssd_volume_uuid
    with ctx.db.read() as conn:
        row = conn.execute(
            "SELECT id FROM destination WHERE kind = 'ssd' AND volume_uuid = ?", (uuid,)
        ).fetchone()
        ssd_id = -1 if row is None else int(row[0])
        return int(
            conn.execute(
                "SELECT count(*) FROM replica r WHERE r.dest_id = ? AND NOT EXISTS "
                "(SELECT 1 FROM replica o WHERE o.dest_id = ? AND o.content_hash = r.content_hash)",
                (mac.id, ssd_id),
            ).fetchone()[0]
        )


def replicate_to_ssd(
    ctx: AppContext, ctl: JobControl, progress: _Progress, clock: Clock | None = None
) -> None:
    """After the pull: bring the SSD and the Mac to the same content, or note what is pending."""
    if not ctx.config.backup.ssd_volume_uuid:
        return  # no SSD chosen in setup: the Mac is the only destination
    target = mac_target(ctx.db, ctx.config)
    mac = Dest(target.dest_id, "mac", target.root)
    ssd = ssd_dest(ctx)
    if ssd is None or not REPLICATION_LOCK.acquire(blocking=False):
        pending_now = ssd_pending(ctx, mac)
        if pending_now:
            progress.warnings.append(f"Mac done, SSD pending: {pending_now} files.")
        return
    try:
        ctl.set_state(JobState.replicating)
        to_ssd, to_mac = Replicator(ctx.db, clock=clock).replicate_union(
            mac, ssd, should_stop=lambda: ctl.stop_requested
        )
    finally:
        REPLICATION_LOCK.release()
    corrupt = to_ssd.corrupt + to_mac.corrupt
    failed = to_ssd.failed + to_mac.failed
    if corrupt:
        progress.warnings.append(
            f"{corrupt} backed-up files did not match their checksum. See Backup, Problems."
        )
    if failed:
        progress.warnings.append(
            f"{failed} files could not be copied to the SSD; they will be tried again."
        )


def replicate_now(ctx: AppContext, should_stop: Callable[[], bool]) -> None:
    """Plugging the SSD in: bring both destinations to the same content, without a phone."""
    if not ctx.config.backup.mac_root:
        return
    target = mac_target(ctx.db, ctx.config)
    mac = Dest(target.dest_id, "mac", target.root)
    ssd = ssd_dest(ctx)
    if ssd is None or not REPLICATION_LOCK.acquire(blocking=False):
        return  # a backup run is replicating already
    try:
        ctx.bus.publish("replication", {"state": "running"})
        to_ssd, to_mac = Replicator(ctx.db).replicate_union(mac, ssd, should_stop=should_stop)
        ctx.bus.publish(
            "replication",
            {
                "state": "done",
                "copied": to_ssd.copied + to_mac.copied,
                "bytes": to_ssd.bytes + to_mac.bytes,
                "problems": to_ssd.corrupt + to_mac.corrupt,
                "failed": to_ssd.failed + to_mac.failed,
            },
        )
    finally:
        REPLICATION_LOCK.release()
        ctx.db.release_thread()


def run_backup(
    ctx: AppContext,
    ctl: JobControl,
    sets: Sequence[str],
    dry_run: bool,
    *,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
    reconnect_pause_s: float = RECONNECT_PAUSE_S,
) -> PullStats | None:
    """Back up `sets` from the job's phone. Returns the pull's counts (None if nothing was pulled).

    Whatever ends the run (done, stopped, discarded, failed), a real run leaves one
    `run_summary` row with its counts and scan warnings; a dry run leaves none.

    `sleep` is the scanner's wait before looking at recent files again, and
    `reconnect_pause_s` the puller's pause after a disconnect (tests shorten both).
    """
    clock = clock or SystemClock()
    started = clock.now()
    progress = _Progress()
    outcome = "failed"
    try:
        result = _run(ctx, ctl, sets, dry_run, clock, sleep, reconnect_pause_s, progress)
        if not dry_run and not ctl.stop_requested:
            replicate_to_ssd(ctx, ctl, progress, clock)
        # A stop asked for just as the run ended is still a stop, not a plain "done".
        outcome = "stopped" if ctl.stop_requested else "done"
        return result
    except JobStopped:
        outcome = "stopped"
        raise
    finally:
        if not dry_run:
            record_summary(
                ctx.db,
                job_id=ctl.job_id,
                device_id=progress.device_id,
                sets=list(sets),
                outcome=outcome,
                discarded=progress.discarded,
                started_at=started,
                finished_at=clock.now(),
                stats=progress.stats,
                warnings=progress.warnings,
            )


def _run(
    ctx: AppContext,
    ctl: JobControl,
    sets: Sequence[str],
    dry_run: bool,
    clock: Clock,
    sleep: Callable[[float], None],
    reconnect_pause_s: float,
    progress: _Progress,
) -> PullStats | None:
    config, db = ctx.config, ctx.db
    try:
        roots = resolve_roots(config.backup.sets, sets)
    except ValueError:
        raise JobRefused(UNKNOWN_SET_MESSAGE) from None
    names = list(dict.fromkeys(r.set_name for r in roots))
    rules = SkipRules.from_config(config)

    def scan_once(session: DeviceSession) -> ScanResult:
        if progress.device_id is None:  # the summary belongs to this phone however the run ends
            progress.device_id = _stored_device_id(db, session.device_uuid)
        try:
            return scan(
                session,
                db,
                roots,
                rules,
                clock=clock,
                settle_s=config.backup.settle_seconds,
                sleep=sleep,
            )
        except DeviceTimeout:
            # A stall is treated like a disconnect: wait for the phone, then scan again.
            log.warning("The phone stopped responding during the scan; it is scanned again")
            raise

    ctl.set_state(JobState.scanning)
    result = ctl.on_phone(scan_once, disconnect_on=(DeviceDisconnected, DeviceTimeout))
    for warning in result.warnings:
        log.warning("Scan: %s", warning)
    progress.warnings.extend(result.warnings)
    if dry_run:
        ctl.report(0, 0, result.new_or_changed, result.bytes_needed)
        return None
    if result.state == "awaiting_confirmation":
        ctl.set_state(JobState.awaiting_confirmation)
        ctl.wait_until(lambda: _scan_state(db, result.scan_id) != "awaiting_confirmation")
        if _scan_state(db, result.scan_id) != "applied":
            log.info("The owner discarded scan %d; nothing is copied", result.scan_id)
            progress.discarded = True
            return None

    session = ctl.wait_for_phone()
    device_id = _device_id(db, session.device_uuid)
    progress.device_id = device_id
    items = pending_items(db, device_id, names)
    target = mac_target(db, config, session.device_uuid)
    problem = space_check(
        sum(i.size for i in items), target.root, config.backup.free_space_headroom_pct
    )
    if problem is not None:
        raise JobRefused(problem)

    ctl.set_state(JobState.pulling)
    puller = Puller(
        db,
        target,
        device_id=device_id,
        clock=clock,
        describe=describe,
        readers=reader_count(config, session),
        reconnect_pause_s=reconnect_pause_s,
    )
    stats = puller.run(ctl, items, progress.stats)
    log.info(
        "Backup: %s",
        json.dumps({k: v for k, v in stats.__dict__.items() if k != "warnings"}),
    )
    return stats
