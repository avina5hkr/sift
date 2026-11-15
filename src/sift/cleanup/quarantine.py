"""Quarantine (spec 10): deleted junk and near-duplicate copies stay restorable for 30 days.

After a run with disposition quarantine, each deleted content that is labelled junk or was a
near-duplicate copy, and has no present phone path left on any phone, has its replicas renamed
into `DEST_ROOT/.quarantine/YYYY-MM-DD/REL_PATH` on every mounted destination (same volume, so a
rename). The replica row moves with the file (plan F3). Purge removes quarantined files past
purge_after from mounted destinations, then their replica rows. Restore moves a file back into
the tree, and for "phone" first pushes it to its original path (a clash gets `_restored`), with
its original mtime, after checking the phone has room for it plus 2 GB.
These are the only places Sift removes or moves a file inside a backup destination, and only
for files the owner deleted from the phone and chose to quarantine.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sift.backup.layout import rel_key
from sift.backup.lease import DRIVE_LEASE
from sift.backup.phonepaths import from_db, to_db
from sift.backup.run import usable_dests
from sift.cleanup.errors import RestoreRefused
from sift.core.context import AppContext
from sift.device.session import DeviceSession

log = logging.getLogger("sift.cleanup")

DAY_S = 86_400
ROOM_BYTES = 2 * 1024**3


def _free_name(root: Path, rel: str, suffix: str = "_restored") -> str:
    """`rel` if nothing is there, else `name_restored.ext`, `name_restored2.ext`, …"""
    if not (root / rel).exists():
        return rel
    p = PurePosixPath(rel)
    for n in range(1, 1000):
        tag = suffix if n == 1 else f"{suffix}{n}"
        candidate = str(p.with_name(f"{p.stem}{tag}{p.suffix}"))
        if not (root / candidate).exists():
            return candidate
    raise OSError(f"No free name for {rel}")


def _move(root: Path, src_rel: str, dst_rel: str) -> None:
    """Rename within the backup folder, never over a file (I1), atomically where the volume
    allows (the backup's own no-replace rename), so a file appearing meanwhile is never lost."""
    from sift.backup.staging import TargetExists, _rename_no_replace

    dst = root / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        _rename_no_replace(root / src_rel, dst)
    except TargetExists as e:
        raise FileExistsError(str(dst)) from e


def quarantine_run(ctx: AppContext, run_id: int) -> int:
    """Move the replicas of this run's quarantinable contents; returns how many files moved."""
    days = ctx.config.cleanup.quarantine_days
    now = int(time.time())
    day = time.strftime("%Y-%m-%d")
    usable = {d.id: d for d in usable_dests(ctx)}
    with ctx.db.read() as conn:
        hashes = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT i.content_hash FROM delete_item i WHERE i.run_id = ? "
                "AND i.state = 'deleted' AND i.content_hash IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM phone_file p WHERE p.content_hash = i.content_hash "
                "AND p.present = 1) "
                "AND (CASE (SELECT source FROM delete_run WHERE id = i.run_id) "
                "WHEN 'slate' THEN NOT EXISTS (SELECT 1 FROM delete_item j WHERE "
                "j.run_id = i.run_id AND j.content_hash = i.content_hash AND j.quarantine = 0) "
                "ELSE ((SELECT source FROM delete_run WHERE id = i.run_id) = 'browse' "
                "OR EXISTS (SELECT 1 FROM label l WHERE l.content_hash = i.content_hash "
                "AND l.label = 'junk') OR EXISTS (SELECT 1 FROM near_member m "
                "WHERE m.content_hash = i.content_hash AND m.role = 'copy')) END)",
                (run_id,),
            )
        ]
        replicas = [
            (str(h), int(d), str(rel))
            for h in hashes
            for h, d, rel in conn.execute(
                "SELECT content_hash, dest_id, rel_path FROM replica WHERE content_hash = ? "
                "AND rel_path NOT LIKE '.quarantine/%'",
                (h,),
            )
        ]
    moved = 0
    with DRIVE_LEASE.hold("quarantine"):  # no copy to or from these folders while files move
        for digest, dest_id, rel in replicas:
            dest = usable.get(dest_id)
            if dest is None:
                continue  # an unplugged drive keeps its copy in the tree
            root = Path(dest.root)
            q_rel = _free_name(root, f".quarantine/{day}/{rel}")
            with ctx.db.tx() as conn:  # the row first: a crash after it is repaired at start
                cur = conn.execute(
                    "INSERT INTO quarantine (content_hash, dest_id, orig_rel, q_rel, moved_at, "
                    "purge_after) VALUES (?, ?, ?, ?, ?, ?)",
                    (digest, dest_id, rel, q_rel, now, now + days * DAY_S),
                )
                qid = int(cur.lastrowid or 0)
            try:
                _move(root, rel, q_rel)
            except OSError as e:
                log.warning("Could not quarantine %s: %s", rel, e)
                with ctx.db.tx() as conn:
                    conn.execute("DELETE FROM quarantine WHERE id = ?", (qid,))
                continue
            with ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE replica SET rel_path = ?, rel_key = ? WHERE dest_id = ? "
                    "AND rel_path = ?",
                    (q_rel, rel_key(q_rel), dest_id, rel),
                )
            moved += 1
    return moved


def _active(conn: object) -> list[tuple[int, str, int, str, str]]:
    return [
        (int(a), str(b), int(c), str(d), str(e))
        for a, b, c, d, e in conn.execute(  # type: ignore[attr-defined]
            "SELECT id, content_hash, dest_id, orig_rel, q_rel FROM quarantine "
            "WHERE purged_at IS NULL AND restored_at IS NULL AND restore_to IS NULL"
        )
    ]


def reconcile(ctx: AppContext) -> int:
    """At start: finish or undo a quarantine move a crash interrupted; returns rows fixed."""
    usable = {d.id: d for d in usable_dests(ctx)}
    fixed = 0
    fixed += _reconcile_restores(ctx, usable)
    with DRIVE_LEASE.hold("quarantine"), ctx.db.tx() as conn:
        for qid, _digest, dest_id, orig, q_rel in _active(conn):
            dest = usable.get(dest_id)
            if dest is None:
                continue
            root = Path(dest.root)
            at_q, at_orig = (root / q_rel).exists(), (root / orig).exists()
            row = conn.execute(
                "SELECT rel_path FROM replica WHERE dest_id = ? AND rel_path IN (?, ?)",
                (dest_id, orig, q_rel),
            ).fetchone()
            if not at_q and at_orig:  # never moved: forget the row
                conn.execute("DELETE FROM quarantine WHERE id = ?", (qid,))
                fixed += 1
            elif at_q and row is not None and row[0] == orig:  # moved, row not updated
                conn.execute(
                    "UPDATE replica SET rel_path = ?, rel_key = ? WHERE dest_id = ? "
                    "AND rel_path = ?",
                    (q_rel, rel_key(q_rel), dest_id, orig),
                )
                fixed += 1
    return fixed


def _reconcile_restores(ctx: AppContext, usable: dict[int, Any]) -> int:
    """Restores a crash interrupted: the file is at its recorded destination (moved, database
    not updated: finish), or still in quarantine (not moved: forget the plan). Either way the
    bytes stay where they are and the replica row ends up pointing at them."""
    fixed = 0
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT id, dest_id, q_rel, restore_to FROM quarantine WHERE restore_to IS NOT NULL "
            "AND purged_at IS NULL AND restored_at IS NULL"
        ).fetchall()
    with DRIVE_LEASE.hold("quarantine"):
        for qid, dest_id, q_rel, back in rows:
            dest = usable.get(int(dest_id))
            if dest is None:
                continue  # repaired when that drive is plugged in
            root = Path(dest.root)
            at_q, at_back = (root / q_rel).is_file(), (root / back).is_file()
            if at_back and not at_q:
                _finish_restore(ctx, int(qid), int(dest_id), str(q_rel), str(back))
                fixed += 1
            elif at_q:
                with ctx.db.tx() as conn:
                    conn.execute("UPDATE quarantine SET restore_to = NULL WHERE id = ?", (qid,))
                fixed += 1
            else:
                log.warning("Quarantined file %s is in neither place; left for review", q_rel)
    return fixed


def revive(ctx: AppContext) -> int:
    """Content that is on a phone again leaves quarantine (moved back into the tree), so its
    backup is never purged and counts again (review M8 C1); returns how many moved back."""
    with ctx.db.read() as conn:
        back = [
            int(r[0])
            for r in conn.execute(
                "SELECT q.id FROM quarantine q WHERE q.purged_at IS NULL AND q.restored_at IS NULL "
                "AND EXISTS (SELECT 1 FROM phone_file p WHERE p.content_hash = q.content_hash "
                "AND p.present = 1)"
            )
        ]
    if not back:
        return 0
    usable = {d.id for d in usable_dests(ctx)}
    ready = [e.id for e in entries(ctx) if e.id in back and e.dest_id in usable]
    if not ready:
        return 0
    try:
        return restore(ctx, ready, "backup")["restored"]
    except Exception:  # never let one bad entry stop every check and purge
        log.exception("Could not bring quarantined files back")
        return 0


def purge_due(ctx: AppContext, *, now: float | None = None) -> int:
    """Remove quarantined files past purge_after on mounted destinations; returns how many."""
    at = int(now if now is not None else time.time())
    revive(ctx)
    usable = {d.id: d for d in usable_dests(ctx)}
    with ctx.db.read() as conn:
        due = conn.execute(
            "SELECT q.id, q.dest_id, q.q_rel FROM quarantine q WHERE q.purge_after <= ? "
            "AND q.purged_at IS NULL AND q.restored_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM phone_file p WHERE p.content_hash = q.content_hash "
            "AND p.present = 1)",
            (at,),
        ).fetchall()
    purged = 0
    with DRIVE_LEASE.hold("quarantine"):
        purged = _purge(ctx, due, usable, at)
    return purged


def _purge(ctx: AppContext, due: list[Any], usable: dict[int, Any], at: int) -> int:
    purged = 0
    for qid, dest_id, q_rel in due:
        dest = usable.get(int(dest_id))
        if dest is None:
            continue  # purged when that drive is mounted
        base = (Path(dest.root) / ".quarantine").resolve()
        path = (Path(dest.root) / str(q_rel)).resolve()
        if not str(q_rel).startswith(".quarantine/") or base not in path.parents:
            log.error("Refusing to purge %s: not inside .quarantine", q_rel)
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Could not purge %s: %s", q_rel, e)
            continue
        with ctx.db.tx() as conn:
            conn.execute("DELETE FROM replica WHERE dest_id = ? AND rel_path = ?", (dest_id, q_rel))
            conn.execute("UPDATE quarantine SET purged_at = ? WHERE id = ?", (at, qid))
        purged += 1
    return purged


@dataclass(frozen=True)
class Entry:
    id: int
    content_hash: str
    dest_id: int
    orig_rel: str
    q_rel: str
    moved_at: int
    purge_after: int
    size: int
    phone_path: str | None


def entries(ctx: AppContext) -> list[Entry]:
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT q.id, q.content_hash, q.dest_id, q.orig_rel, q.q_rel, q.moved_at, "
            "q.purge_after, IFNULL(c.size, 0), (SELECT d.remote_path FROM deletion_log d "
            "WHERE d.content_hash = q.content_hash AND d.result = 'deleted' "
            "ORDER BY d.id DESC LIMIT 1) FROM quarantine q LEFT JOIN content c "
            "ON c.hash = q.content_hash WHERE q.purged_at IS NULL AND q.restored_at IS NULL "
            "ORDER BY q.moved_at DESC, q.id"
        ).fetchall()
    return [
        Entry(int(a), str(b), int(c), str(d), str(e), int(f), int(g), int(h),
              from_db(str(i)) if i else None)
        for a, b, c, d, e, f, g, h, i in rows
    ]  # fmt: skip


def restore(
    ctx: AppContext, ids: list[int], to: str, session: DeviceSession | None = None
) -> dict[str, int]:
    """Restore quarantined files: to the backup only, or to the phone and the backup."""
    chosen = [e for e in entries(ctx) if e.id in set(ids)]
    usable = {d.id: d for d in usable_dests(ctx)}
    if any(e.dest_id not in usable for e in chosen):
        raise RestoreRefused("Plug in the drive that holds these files first.")
    phone_done: set[str] = set()
    if to == "phone":
        if session is None:
            raise RestoreRefused("Connect the phone over USB first.")
        _same_phone(ctx, session, chosen)
        need = sum(e.size for e in {e.content_hash: e for e in chosen}.values())
        free = session.read_info().free_bytes
        if free is None or free < need + ROOM_BYTES:
            raise RestoreRefused("The phone needs room for these files plus 2 GB.")
    restored = pushed = 0
    with DRIVE_LEASE.hold("quarantine"):
        for e in chosen:
            done = _restore_one(ctx, e, to, session, usable, phone_done)
            restored += 1
            pushed += done
    return {"restored": restored, "pushed": pushed}


def _restore_one(
    ctx: AppContext,
    e: Entry,
    to: str,
    session: DeviceSession | None,
    usable: dict[int, Any],
    phone_done: set[str],
) -> int:
    pushed = 0
    root = Path(usable[e.dest_id].root)
    if not (root / e.q_rel).is_file():  # removed by hand: nothing to restore, forget it
        log.warning("Quarantined file %s is missing; its entry is closed", e.q_rel)
        with ctx.db.tx() as conn:
            conn.execute(
                "DELETE FROM replica WHERE dest_id = ? AND rel_path = ?", (e.dest_id, e.q_rel)
            )
            conn.execute(
                "UPDATE quarantine SET purged_at = ? WHERE id = ?", (int(time.time()), e.id)
            )
        return 0
    if to == "phone" and e.content_hash not in phone_done and e.phone_path:
        _push_back(ctx, session, root / e.q_rel, e)  # type: ignore[arg-type]
        phone_done.add(e.content_hash)
        pushed = 1
    back = _free_name(root, e.orig_rel)
    with ctx.db.tx() as conn:  # where it goes, first: a crash after the move is repaired
        conn.execute("UPDATE quarantine SET restore_to = ? WHERE id = ?", (back, e.id))
    _move(root, e.q_rel, back)
    _finish_restore(ctx, e.id, e.dest_id, e.q_rel, back)
    return pushed


def _finish_restore(ctx: AppContext, qid: int, dest_id: int, q_rel: str, back: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE replica SET rel_path = ?, rel_key = ? WHERE dest_id = ? AND rel_path = ?",
            (back, rel_key(back), dest_id, q_rel),
        )
        conn.execute(
            "UPDATE quarantine SET restored_at = ?, restore_to = NULL WHERE id = ?",
            (int(time.time()), qid),
        )


def _same_phone(ctx: AppContext, session: DeviceSession, chosen: list[Entry]) -> None:
    """Restore to a phone only goes back to the phone the files were deleted from."""
    with ctx.db.read() as conn:
        owners = {
            str(r[0])
            for e in chosen
            for r in conn.execute(
                "SELECT d.uuid FROM deletion_log l JOIN phone_file p ON p.id = l.phone_file_id "
                "JOIN device d ON d.id = p.device_id WHERE l.content_hash = ? "
                "AND l.result = 'deleted'",
                (e.content_hash,),
            )
        }
    if owners and owners != {session.device_uuid}:
        raise RestoreRefused("Connect the phone these files were deleted from.")
    try:
        session.check_identity()
    except Exception as exc:
        raise RestoreRefused(f"Sift could not confirm the phone: {exc}") from exc


def _push_back(ctx: AppContext, session: DeviceSession, local: Path, e: Entry) -> None:
    assert e.phone_path is not None
    transport = session.transport
    with ctx.db.read() as conn:
        row = conn.execute(
            "SELECT id, mtime, device_id FROM phone_file "
            "WHERE remote_path = ? AND state = 'deleted' "
            "ORDER BY id DESC LIMIT 1",
            (to_db(e.phone_path),),
        ).fetchone()
    mtime = int(row[1]) if row else int(local.stat().st_mtime)
    from sift.cleanup.archive import push_verified

    target = push_verified(session, local, e.phone_path, mtime, e.content_hash)
    transport.media_rescan([target])
    with ctx.db.tx() as conn:
        if row is not None and target == e.phone_path:
            conn.execute(
                "UPDATE phone_file SET state = 'done', present = 1 WHERE id = ?", (int(row[0]),)
            )
        elif row is not None:
            conn.execute(
                "INSERT OR IGNORE INTO phone_file (device_id, remote_path, origin, size, mtime, "
                "content_hash, state, present) SELECT device_id, ?, origin, size, ?, content_hash, "
                "'done', 1 FROM phone_file WHERE id = ?",
                (to_db(target), mtime, int(row[0])),
            )
