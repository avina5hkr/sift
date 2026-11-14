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
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sift.backup.layout import rel_key
from sift.backup.phonepaths import from_db, to_db
from sift.backup.run import usable_dests
from sift.core.context import AppContext
from sift.device.session import DeviceSession

log = logging.getLogger("sift.cleanup")

DAY_S = 86_400
ROOM_BYTES = 2 * 1024**3


class RestoreRefused(Exception):
    pass


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
    dst = root / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():  # never replace a file (I1)
        raise FileExistsError(str(dst))
    os.rename(root / src_rel, dst)


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
                "AND (EXISTS (SELECT 1 FROM label l WHERE l.content_hash = i.content_hash "
                "AND l.label = 'junk') OR EXISTS (SELECT 1 FROM near_member m "
                "WHERE m.content_hash = i.content_hash AND m.role = 'copy'))",
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
    for digest, dest_id, rel in replicas:
        dest = usable.get(dest_id)
        if dest is None:
            continue  # an unplugged drive keeps its copy in the tree
        root = Path(dest.root)
        q_rel = _free_name(root, f".quarantine/{day}/{rel}")
        try:
            _move(root, rel, q_rel)
        except OSError as e:
            log.warning("Could not quarantine %s: %s", rel, e)
            continue
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE replica SET rel_path = ?, rel_key = ? WHERE dest_id = ? AND rel_path = ?",
                (q_rel, rel_key(q_rel), dest_id, rel),
            )
            conn.execute(
                "INSERT INTO quarantine (content_hash, dest_id, orig_rel, q_rel, moved_at, "
                "purge_after) VALUES (?, ?, ?, ?, ?, ?)",
                (digest, dest_id, rel, q_rel, now, now + days * DAY_S),
            )
        moved += 1
    return moved


def purge_due(ctx: AppContext, *, now: float | None = None) -> int:
    """Remove quarantined files past purge_after on mounted destinations; returns how many."""
    at = int(now if now is not None else time.time())
    usable = {d.id: d for d in usable_dests(ctx)}
    with ctx.db.read() as conn:
        due = conn.execute(
            "SELECT id, dest_id, q_rel FROM quarantine WHERE purge_after <= ? "
            "AND purged_at IS NULL AND restored_at IS NULL",
            (at,),
        ).fetchall()
    purged = 0
    for qid, dest_id, q_rel in due:
        dest = usable.get(int(dest_id))
        if dest is None:
            continue  # purged when that drive is mounted
        path = Path(dest.root) / str(q_rel)
        if not str(q_rel).startswith(".quarantine/"):
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
        need = sum(e.size for e in {e.content_hash: e for e in chosen}.values())
        free = session.read_info().free_bytes
        if free is None or free < need + ROOM_BYTES:
            raise RestoreRefused("The phone needs room for these files plus 2 GB.")
    restored = pushed = 0
    for e in chosen:
        root = Path(usable[e.dest_id].root)
        if to == "phone" and e.content_hash not in phone_done and e.phone_path:
            _push_back(ctx, session, root / e.q_rel, e)  # type: ignore[arg-type]
            phone_done.add(e.content_hash)
            pushed += 1
        back = _free_name(root, e.orig_rel)
        _move(root, e.q_rel, back)
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE replica SET rel_path = ?, rel_key = ? WHERE dest_id = ? AND rel_path = ?",
                (back, rel_key(back), e.dest_id, e.q_rel),
            )
            conn.execute(
                "UPDATE quarantine SET restored_at = ? WHERE id = ?", (int(time.time()), e.id)
            )
        restored += 1
    return {"restored": restored, "pushed": pushed}


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
    target = e.phone_path
    if transport.stat(target) is not None:
        p = PurePosixPath(target)
        target = str(p.with_name(f"{p.stem}_restored{p.suffix}"))
    transport.push(local, target, mtime)
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
