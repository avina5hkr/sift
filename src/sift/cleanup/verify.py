"""Fresh content checks of backup copies, for anything that relies on a backup being good:
deleting the phone's copy (I3a) and restoring to the phone.

A replica row's `verified_at` and size say the copy was right when it was written or last
verified; they do not prove the bytes are still right. Every check here re-reads the copy from
the disk (cache off, the final path component never followed as a symlink) and compares its
SHA-256 with the content hash. Callers decide the window: the delete checks hash every required
copy once, and the delete itself hashes them again just before each file is deleted.
"""

import logging
from pathlib import Path

from sift.backup.confirm import current_dest_ids, required_kinds
from sift.backup.run import usable_dests
from sift.backup.staging import regular_file_sha256
from sift.core.context import AppContext

log = logging.getLogger("sift.cleanup")

Cache = dict[tuple[int, str], str | None]  # (dest id, rel path) -> hash read in this run


def local_matches(root: Path, rel: str, digest: str, size: int, cache: Cache | None = None,
                  dest_id: int = 0) -> bool:  # fmt: skip
    """The file at `root/rel` is a regular file inside `root` with this size and SHA-256."""
    base = root.resolve()
    path = root / rel
    try:
        if base not in path.resolve().parents:  # a symlinked folder leading out of the backup
            return False
        if path.stat().st_size != size:
            return False
        key = (dest_id, rel)
        if cache is not None and key in cache:
            got = cache[key]
        else:
            got = regular_file_sha256(path)
            if cache is not None:
                cache[key] = got
    except OSError:
        return False
    if got != digest:
        log.warning("The backup copy %s does not match its content (damaged or changed)", rel)
        return False
    return True


def required_copies(ctx: AppContext, digest: str | None, size: int,
                    cache: Cache | None = None) -> str | None:  # fmt: skip
    """None when every required backup folder holds a good copy of this content now; else the
    reason: not_backed_up, waiting_for_ssd, backup_missing or backup_corrupt."""
    if digest is None:
        return "not_backed_up"
    cfg = ctx.config
    dests = current_dest_ids(ctx.db, cfg)
    usable = {d.id: d for d in usable_dests(ctx)}
    with ctx.db.read() as conn:
        for kind in required_kinds(cfg):
            dest_id = dests[kind]
            row = conn.execute(
                "SELECT rel_path, size FROM replica WHERE content_hash = ? AND dest_id = ? "
                "AND verified_at IS NOT NULL AND rel_path NOT LIKE '.quarantine/%'",
                (digest, dest_id),
            ).fetchone()
            if row is None:
                return "not_backed_up"
            dest = usable.get(dest_id)
            if dest is None:
                return "waiting_for_ssd" if kind == "ssd" else "backup_missing"
            if int(row[1]) != size:
                return "backup_missing"
            root = Path(dest.root)
            rel = str(row[0])
            try:
                if (root / rel).stat().st_size != size:
                    return "backup_missing"  # gone or the wrong size
            except OSError:
                return "backup_missing"
            if not local_matches(root, rel, digest, size, cache, dest_id):
                return "backup_corrupt"
    return None
