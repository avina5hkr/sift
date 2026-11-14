"""The Archive browser (spec 10A): the backup tree on a mounted destination, and restore to phone.

Lists the files Sift has replica rows for on that destination (plan G4), outside `.quarantine`.
Restore pushes each chosen file to its original phone path with its original mtime (a clash gets
`_restored`), after checking the phone is the one the content came from and has room for the
selection plus 2 GB; then a media rescan. A file whose content is already present on the phone
at that path is skipped.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sift.backup.phonepaths import from_db, to_db
from sift.backup.run import usable_dests
from sift.cleanup.errors import RestoreRefused
from sift.core.context import AppContext
from sift.device.session import DeviceSession
from sift.device.transport import RemoteError, TransportError

log = logging.getLogger("sift.cleanup")

ROOM_BYTES = 2 * 1024**3


@dataclass
class ArchiveFile:
    rel_path: str
    name: str
    size: int
    content_hash: str
    phone_path: str | None
    on_phone: bool


@dataclass
class ArchiveFolder:
    name: str
    rel_path: str
    size: int = 0
    files: int = 0


def listing(
    ctx: AppContext, dest_id: int, rel: str
) -> tuple[list[ArchiveFolder], list[ArchiveFile]]:
    usable = {d.id for d in usable_dests(ctx)}
    if dest_id not in usable:
        raise RestoreRefused("That backup drive is not connected.")
    base = rel.strip("/")
    prefix = f"{base}/" if base else ""
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT r.rel_path, r.size, r.content_hash, "
            "(SELECT p.remote_path FROM phone_file p WHERE p.content_hash = r.content_hash "
            "ORDER BY p.present DESC, p.id DESC LIMIT 1), "
            "EXISTS (SELECT 1 FROM phone_file p WHERE p.content_hash = r.content_hash "
            "AND p.present = 1) FROM replica r WHERE r.dest_id = ? "
            "AND r.rel_path NOT LIKE '.quarantine/%' AND r.rel_path LIKE ? ESCAPE '!'",
            (dest_id, prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"),
        ).fetchall()
    folders: dict[str, ArchiveFolder] = {}
    files: list[ArchiveFile] = []
    for rel_path, size, digest, phone, present in rows:
        rest = str(rel_path)[len(prefix) :]
        if "/" in rest:
            name = rest.split("/", 1)[0]
            f = folders.setdefault(name, ArchiveFolder(name, prefix + name))
            f.size += int(size)
            f.files += 1
        else:
            files.append(
                ArchiveFile(str(rel_path), rest, int(size), str(digest),
                            from_db(str(phone)) if phone else None, bool(present))
            )  # fmt: skip
    return sorted(folders.values(), key=lambda f: f.name.casefold()), sorted(
        files, key=lambda f: f.name.casefold()
    )


def restore(
    ctx: AppContext,
    dest_id: int,
    rel_paths: list[str],
    session: DeviceSession | None,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    usable = {d.id: d for d in usable_dests(ctx)}
    dest = usable.get(dest_id)
    if dest is None:
        raise RestoreRefused("That backup drive is not connected.")
    if session is None:
        raise RestoreRefused("Connect the phone over USB first.")
    with ctx.db.read() as conn:
        plan = []
        for rel in dict.fromkeys(rel_paths):  # each file once
            row = conn.execute(
                "SELECT r.content_hash, r.size, p.id, p.remote_path, p.mtime, p.present, d.uuid "
                "FROM replica r JOIN phone_file p ON p.content_hash = r.content_hash "
                "JOIN device d ON d.id = p.device_id WHERE r.dest_id = ? AND r.rel_path = ? "
                "ORDER BY p.present DESC, p.id DESC LIMIT 1",
                (dest_id, rel),
            ).fetchone()
            if row is not None:
                plan.append((rel, *row))
    if any(r[7] != session.device_uuid for r in plan):
        raise RestoreRefused("Connect the phone these files were backed up from.")
    try:
        session.check_identity()
    except Exception as e:
        raise RestoreRefused(f"Sift could not confirm the phone: {e}") from e
    todo = [r for r in plan if not r[6]]  # already on the phone: nothing to do
    free = session.read_info().free_bytes
    need = sum(int(r[2]) for r in todo)
    if free is None or free < need + ROOM_BYTES:
        raise RestoreRefused("The phone needs room for these files plus 2 GB.")
    pushed: list[str] = []
    failed = 0
    for n, (rel, digest, size, pid, stored, mtime, _present, _uuid) in enumerate(todo, 1):
        if should_stop():
            break
        local = Path(dest.root) / rel
        try:
            if local.stat().st_size != int(size):
                raise OSError("the backup copy is not the right size")
            target = push_verified(session, local, from_db(str(stored)), int(mtime), str(digest))
        except RestoreRefused as e:
            log.warning("Did not restore %s: %s", rel, e)
            failed += 1
            continue
        except (OSError, RemoteError) as e:
            log.warning("Could not restore %s: %s", rel, e)
            failed += 1
            continue
        except TransportError as e:  # the phone went away: stop, report what was done
            log.warning("Restore stopped: %s", e)
            break
        finally:
            if on_progress is not None:
                on_progress(n, len(todo))
        pushed.append(target)
        with ctx.db.tx() as conn:
            if target == from_db(str(stored)):
                conn.execute(
                    "UPDATE phone_file SET state = 'done', present = 1 WHERE id = ?", (int(pid),)
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO phone_file (device_id, remote_path, origin, size, "
                    "mtime, content_hash, state, present) SELECT device_id, ?, origin, size, ?, "
                    "content_hash, 'done', 1 FROM phone_file WHERE id = ?",
                    (to_db(target), int(mtime), int(pid)),
                )
    if pushed:
        try:
            session.transport.media_rescan(pushed)
        except Exception as e:  # best effort
            log.info("Media rescan failed: %s", e)
    return {
        "restored": len(pushed),
        "already_on_phone": len(plan) - len(todo),
        "failed": failed,
        "not_done": len(todo) - len(pushed) - failed,
    }


def push_one(session: DeviceSession, local: Path, phone_path: str, mtime: int) -> str:
    """Push to `phone_path`, or to a free `_restored` name beside it; returns where it went.
    For files with no content hash to check (a contacts file); backups use `push_verified`."""
    target = _free_phone_name(session.transport, phone_path)
    session.transport.push(local, target, mtime)
    return target


def push_verified(
    session: DeviceSession, local: Path, phone_path: str, mtime: int, digest: str
) -> str:
    """Restore one backed-up file to the phone and prove it arrived intact.

    The backup copy is hashed first (a damaged copy is never restored); it is pushed to
    `phone_path` or a free `_restored` name beside it (never over a file); then the phone hashes
    what it received. A wrong or partial upload is removed (it is the file this call just
    created under a name that was free) and RestoreRefused is raised. Returns where it went."""
    from sift.backup.staging import regular_file_sha256
    from sift.device.transport import DeviceDisconnected, DeviceTimeout

    try:
        source = regular_file_sha256(local)
    except OSError as e:
        raise RestoreRefused(f"The backup copy could not be read: {e}") from e
    if source != digest:
        raise RestoreRefused("The backup copy is damaged (its content does not match).")
    transport = session.transport
    target = _free_phone_name(transport, phone_path)
    try:
        transport.push(local, target, mtime)
    except DeviceDisconnected, DeviceTimeout:
        raise  # the phone went away: whatever arrived is checked by the next restore's stat
    except Exception:
        _remove_own_upload(transport, target)
        raise
    got = transport.sha256([target]).get(target)
    if got != digest:
        _remove_own_upload(transport, target)
        raise RestoreRefused("The phone did not receive the file intact; that copy was removed.")
    return target


def _free_phone_name(transport: Any, phone_path: str) -> str:
    target = phone_path
    p = PurePosixPath(phone_path)
    n = 1
    while transport.stat(target) is not None:  # never over a file on the phone
        target = str(p.with_name(f"{p.stem}{'_restored' if n == 1 else f'_restored{n}'}{p.suffix}"))
        n += 1
        if n > 100:
            raise RestoreRefused(f"No free name for {phone_path} on the phone.")
    return target


def _remove_own_upload(transport: Any, target: str) -> None:
    try:
        if transport.stat(target) is not None:
            transport.delete(target)
    except Exception as e:
        log.warning("Could not remove the failed upload %s: %s", target, e)
