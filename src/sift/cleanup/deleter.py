"""Execute a delete run (spec 10, step 5). The only code that deletes phone files.

Refused unless: the run is 'ready'; its preflight finished less than 10 minutes ago; the owner
confirmed (and typed the count when required); for the suggestion tabs, every passed file is
still ticked; the connected phone is the run's phone; the manifest is written (I9). Then, one
file at a time: a fresh stat (size and mtime unchanged) → `Transport.delete` → stat to confirm it
is gone → the phone_file row is marked deleted → deletion_log. A failure is logged and the run
goes on; a lost phone stops it (the rest stay planned). At most max_delete_per_run files.
Afterwards: a best-effort media rescan, then quarantine (disposition quarantine only).
"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from sift.cleanup.errors import ExecuteRefused
from sift.cleanup.manifest import write_manifests
from sift.cleanup.preflight import selection_hash
from sift.cleanup.quarantine import quarantine_run
from sift.core.context import AppContext
from sift.device.session import DeviceSession
from sift.device.transport import DeviceDisconnected, DeviceTimeout, RemoteError, TransportError

log = logging.getLogger("sift.cleanup")

FRESH_S = 10 * 60


def check_execute(
    ctx: AppContext,
    run_id: int,
    session: DeviceSession | None,
    *,
    confirm: bool,
    typed_count: int | None,
    now: Callable[[], float] = time.time,
) -> None:
    """Raise ExecuteRefused unless this run may delete now."""
    from sift.device.wifi import delete_blocked

    if why := delete_blocked(ctx.config.phone.allow_delete_over_wifi, session):
        raise ExecuteRefused("wifi", why)
    with ctx.db.read() as conn:
        row = conn.execute(
            "SELECT state, preflight_done_at, typed_required, passed, source, selection_hash, "
            "device_id FROM delete_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ExecuteRefused("unknown_run", "Run the checks first.")
        state, done_at, typed, passed, source, sel_hash, device_id = row
        uuid = conn.execute("SELECT uuid FROM device WHERE id = ?", (device_id,)).fetchone()[0]
        ticked = [
            int(r[0])
            for r in conn.execute(
                "SELECT i.phone_file_id FROM delete_item i JOIN selection s "
                "ON s.phone_file_id = i.phone_file_id AND s.checked = 1 "
                "WHERE i.run_id = ? AND i.check_result = 'ok'",
                (run_id,),
            )
        ]
    if state != "ready":
        raise ExecuteRefused(
            "not_ready", "These files are not ready to delete. Run the checks again."
        )
    if not confirm:
        raise ExecuteRefused("not_confirmed", "Confirm that you reviewed these files.")
    if done_at is None or now() - int(done_at) > FRESH_S:
        raise ExecuteRefused(
            "stale_preflight", "The checks are more than 10 minutes old. Run them again."
        )
    if typed and typed_count != int(passed):
        raise ExecuteRefused("typed_count", f"Type the number of files, {passed}, to confirm.")
    if source == "slate":
        from sift.cleanup.preflight import _slate_kept

        with ctx.db.read() as conn:
            ok = {
                int(r[0])
                for r in conn.execute(
                    "SELECT phone_file_id FROM delete_item WHERE run_id = ? "
                    "AND check_result = 'ok'",
                    (run_id,),
                )
            }
        if _slate_kept(ctx, run_id, ok):
            raise ExecuteRefused(
                "plan_changed", "The plan now keeps some of these files. Check again."
            )
    if source == "cleanup" and selection_hash(ticked) != sel_hash:
        raise ExecuteRefused("selection_changed", "The ticked files changed. Run the checks again.")
    from sift.cleanup.preflight import copies_problem

    problem = copies_problem(ctx)
    if problem:
        raise ExecuteRefused("one_copy", problem)
    if session is None:
        raise ExecuteRefused("no_phone", "Connect the phone over USB first.")
    if session.device_uuid != uuid:
        raise ExecuteRefused(
            "wrong_phone", "This is not the phone these files were backed up from."
        )
    try:
        session.check_identity()
    except Exception as e:
        raise ExecuteRefused("wrong_phone", f"Sift could not confirm the phone: {e}") from e


def backups_still_there(ctx: AppContext, digest: str | None, size: int) -> bool:
    """I3a again, just before the delete: each required copy is still there, right size."""
    from pathlib import Path

    from sift.backup.confirm import current_dest_ids, required_kinds
    from sift.backup.run import usable_dests

    if digest is None:
        return False
    dests = current_dest_ids(ctx.db, ctx.config)
    usable = {d.id: d for d in usable_dests(ctx)}
    with ctx.db.read() as conn:
        for kind in required_kinds(ctx.config):
            dest = usable.get(dests[kind])
            row = conn.execute(
                "SELECT rel_path FROM replica WHERE content_hash = ? AND dest_id = ? "
                "AND verified_at IS NOT NULL AND rel_path NOT LIKE '.quarantine/%'",
                (digest, dests[kind]),
            ).fetchone()
            if dest is None or row is None:
                return False
            try:
                if (Path(dest.root) / str(row[0])).stat().st_size != size:
                    return False
            except OSError:
                return False
    return True


STANDARD_FOLDERS = frozenset(
    f"/sdcard/{name}".casefold()
    for name in ("DCIM", "Pictures", "Movies", "Download", "Documents", "Music", "Android")
) | {"/sdcard"}


def conn_source(ctx: AppContext, run_id: int) -> str:
    with ctx.db.read() as conn:
        return str(
            conn.execute("SELECT source FROM delete_run WHERE id = ?", (run_id,)).fetchone()[0]
        )


def remove_empty_folders(transport: Any, deleted: list[str]) -> int:
    """Remove folders the run left empty, deepest first, never a standard top-level folder and
    never one that still lists anything (plan G3); `rmdir` refuses a non-empty folder anyway."""
    from posixpath import dirname

    folders: set[str] = set()
    for path in deleted:
        parent = dirname(path)
        while (
            parent.casefold().startswith("/sdcard/") and parent.casefold() not in STANDARD_FOLDERS
        ):
            folders.add(parent)
            parent = dirname(parent)
    removed = 0
    for folder in sorted(folders, key=lambda p: -p.count("/")):
        try:
            if transport.list_dir(folder):
                continue
            transport.remove_dir(folder)
            removed += 1
        except (RemoteError, TransportError) as e:
            log.info("Left folder %s: %s", folder, e)
    return removed


def execute(
    ctx: AppContext,
    run_id: int,
    session: DeviceSession,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Delete the run's passed files. `check_execute` must have passed just before."""
    write_manifests(ctx, run_id)  # I9: before the first delete; raises when it cannot
    limit = ctx.config.cleanup.max_delete_per_run
    if conn_source(ctx, run_id) == "slate":
        limit = 1_000_000  # Clean slate runs the whole plan, in chunks (spec 10A)
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE delete_run SET state = 'running', started_at = ? WHERE id = ?",
            (int(time.time()), run_id),
        )
        rows = conn.execute(
            "SELECT phone_file_id, remote_path, content_hash, size, mtime FROM delete_item "
            "WHERE run_id = ? AND check_result = 'ok' AND state = 'planned' ORDER BY remote_path "
            "LIMIT ?",
            (run_id, limit),
        ).fetchall()
    from sift.backup.phonepaths import from_db

    transport = session.transport
    deleted = failed = freed = 0
    gone_paths: list[str] = []
    final = "done"
    moved = 0
    try:
        for n, (pid, stored, digest, size, mtime) in enumerate(rows, 1):
            if should_stop():
                final = "stopped"
                break
            path = from_db(str(stored))
            state, error = "deleted", None
            try:
                entry = transport.stat(path)
                if entry is None:
                    state = "gone"
                elif entry.size != size or entry.mtime != mtime:
                    state = "changed"  # edited after the preflight: left alone
                elif not backups_still_there(ctx, digest, int(size)):
                    state, error = "failed", "The backup copy is missing now."
                else:
                    transport.delete(path)
                    if transport.stat(path) is not None:
                        state, error = "failed", "The file is still on the phone."
            except (DeviceDisconnected, DeviceTimeout) as e:
                log.warning("The phone went away during a delete run: %s", e)
                final = "stopped"
                break
            except (RemoteError, TransportError) as e:
                state, error = "failed", str(e)
            now = int(time.time())
            with ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE delete_item SET state = ?, error = ? "
                    "WHERE run_id = ? AND phone_file_id = ?",
                    (state, error, run_id, pid),
                )
                if state == "deleted":
                    conn.execute(
                        "UPDATE phone_file SET state = 'deleted', present = 0 WHERE id = ?", (pid,)
                    )
                    conn.execute("DELETE FROM selection WHERE phone_file_id = ?", (pid,))
                conn.execute(
                    "UPDATE delete_run SET deleted = deleted + ?, failed = failed + ?, "
                    "bytes_freed = bytes_freed + ? WHERE id = ?",
                    (int(state == "deleted"), int(state == "failed"),
                     int(size) if state == "deleted" else 0, run_id),
                )  # fmt: skip
                if state in ("deleted", "failed"):
                    conn.execute(
                        "INSERT INTO deletion_log (phone_file_id, remote_path, content_hash, size, "
                        "deleted_at, result, preflight, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (pid, stored, digest, size, now if state == "deleted" else None, state,
                         json.dumps({"run": run_id}), run_id),
                    )  # fmt: skip
            if state == "deleted":
                deleted += 1
                freed += int(size)
                gone_paths.append(path)
            elif state == "failed":
                failed += 1
            if on_progress is not None:
                on_progress(n, len(rows))
        try:
            if gone_paths and not transport.media_rescan(gone_paths):
                log.info("The phone did not take the media rescan request")
        except Exception as e:  # best effort only (F4)
            log.info("Media rescan failed: %s", e)
        with ctx.db.read() as conn:
            disposition = conn.execute(
                "SELECT disposition FROM delete_run WHERE id = ?", (run_id,)
            ).fetchone()[0]
        moved = quarantine_run(ctx, run_id) if disposition == "quarantine" else 0
        source = conn_source(ctx, run_id)
        empty_too = source == "browse" or (
            source == "slate" and ctx.config.slate.remove_empty_folders
        )
        if empty_too and gone_paths:
            remove_empty_folders(transport, gone_paths)
    except BaseException:
        final = "failed"  # an unexpected error still ends the run with its counts
        raise
    finally:
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE delete_run SET state = ?, finished_at = ? WHERE id = ?",
                (final, int(time.time()), run_id),
            )
    with ctx.db.read() as conn:
        not_done = conn.execute(
            "SELECT count(*) FROM delete_item WHERE run_id = ? AND check_result = 'ok' "
            "AND state = 'planned'",
            (run_id,),
        ).fetchone()[0]
    return {
        "deleted": deleted,
        "failed": failed,
        "bytes_freed": freed,
        "quarantined": moved,
        "not_done": int(not_done),  # stopped, or beyond max_delete_per_run
    }
