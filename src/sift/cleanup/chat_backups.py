"""Saved WhatsApp chat backups in the backup folders: list them by date, delete older ones.

WhatsApp writes a new chat backup every night and Sift never replaces a backup file (I1), so
each version is kept under its own name. This is the owner's way to remove old ones. It is the
only place, besides quarantine purge, that deletes from a backup folder, and only on the
owner's explicit request. Guards, checked again at delete time:

- the newest backup of each app and folder is always kept;
- a copy whose file is still on the phone is kept (it would only be copied again);
- every backup folder holding a copy must be plugged in, so a copy is removed everywhere.
"""

import contextlib
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sift.backup.lease import DRIVE_LEASE
from sift.backup.run import usable_dests
from sift.cleanup.errors import ChatBackupKept as DeleteRefused
from sift.core.context import AppContext

log = logging.getLogger("sift.cleanup")

# Where the chat backups sit in the backup: PHONE_LABEL/<phone path below /sdcard>, so the
# WhatsApp folder must come right after the label (not, say, Documents/WhatsApp/Backups).
_FOLDERS = re.compile(
    r"^[^/]+/(?:android/media/com\.whatsapp(?:\.w4b)?/)?(whatsapp(?: business)?)/"
    r"(databases|backups)/[^/]+$",
    re.I,
)


@dataclass
class Copy:
    content_hash: str
    name: str
    app: str  # WhatsApp or WhatsApp Business
    folder: str  # Databases or Backups
    size: int
    date: int  # the file's own date (the phone's time of the backup), Unix seconds
    drives: list[str] = field(default_factory=list)  # mac, ssd
    on_phone: bool = False
    newest: bool = False
    deletable: bool = False
    why_kept: str | None = None


def _where(rel_path: str) -> tuple[str, str] | None:
    m = _FOLDERS.match(rel_path)
    if m is None:
        return None
    app = "WhatsApp Business" if "business" in m.group(1).lower() else "WhatsApp"
    return app, m.group(2).capitalize()


def listing(ctx: AppContext) -> list[Copy]:
    mounted = {d.id: d for d in usable_dests(ctx)}
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT r.content_hash, r.dest_id, r.rel_path, r.size, r.verified_at, d.kind "
            "FROM replica r JOIN destination d ON d.id = r.dest_id "
            "WHERE r.rel_path NOT LIKE '.quarantine/%' AND (lower(r.rel_path) LIKE "
            "'%/databases/%' OR lower(r.rel_path) LIKE '%/backups/%')"
        ).fetchall()
        on_phone = {
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT content_hash FROM phone_file WHERE present = 1 "
                "AND content_hash IS NOT NULL AND (lower(remote_path) LIKE '%/databases/%' "
                "OR lower(remote_path) LIKE '%/backups/%')"
            )
        }
    copies: dict[str, Copy] = {}
    for digest, dest_id, rel, size, verified_at, kind in rows:
        where = _where(str(rel))
        if where is None:
            continue
        c = copies.get(str(digest))
        if c is None:
            date = int(verified_at or 0)
            dest = mounted.get(int(dest_id))
            if dest is not None:
                with contextlib.suppress(OSError):
                    date = int((Path(dest.root) / str(rel)).stat().st_mtime)
            c = copies[str(digest)] = Copy(
                str(digest), str(rel).rsplit("/", 1)[-1], where[0], where[1], int(size), date
            )
        c.drives.append(str(kind))
        c.on_phone = str(digest) in on_phone
    newest: dict[tuple[str, str], Copy] = {}
    for c in copies.values():
        key = (c.app, c.folder)
        if key not in newest or c.date > newest[key].date:
            newest[key] = c
    for c in copies.values():
        c.newest = newest[(c.app, c.folder)] is c
        if c.newest:
            c.why_kept = "The newest backup is always kept."
        elif c.on_phone:
            c.why_kept = "Still on the phone; WhatsApp removes old ones itself."
        c.deletable = c.why_kept is None
    return sorted(copies.values(), key=lambda c: (c.app, c.folder, -c.date))


def delete(ctx: AppContext, hashes: list[str]) -> dict[str, Any]:
    """Delete these chat backup copies from every backup folder. All or nothing per request:
    any copy that may not be deleted refuses the whole request before a file is touched."""
    with DRIVE_LEASE.hold(
        "chat backup delete"
    ):  # no backup copies to or from the folders meanwhile
        by_hash = {c.content_hash: c for c in listing(ctx)}
        wanted = list(dict.fromkeys(hashes))
        for h in wanted:
            c = by_hash.get(h)
            if c is None:
                raise DeleteRefused("That chat backup is not in the backup folders.")
            if not c.deletable:
                raise DeleteRefused(f"{c.name}: {c.why_kept}")
        mounted = {d.id: d for d in usable_dests(ctx)}
        with ctx.db.read() as conn:
            plan = [
                (h, int(r[0]), str(r[1]))
                for h in wanted
                for r in conn.execute(
                    "SELECT dest_id, rel_path FROM replica WHERE content_hash = ? "
                    "AND rel_path NOT LIKE '.quarantine/%'",
                    (h,),
                )
            ]
        if any(dest_id not in mounted for _, dest_id, _ in plan):
            raise DeleteRefused("Plug in every backup drive first, so the copies go everywhere.")
        deleted = failed = 0
        freed = 0
        for h, dest_id, rel in plan:
            root = Path(mounted[dest_id].root).resolve()
            path = (root / rel).resolve()
            if root not in path.parents or _where(rel) is None:
                log.error("Refusing to delete %s: not a chat backup in the backup folder", rel)
                failed += 1
                continue
            try:
                size = path.stat().st_size if path.exists() else 0
                os.unlink(path) if path.exists() else None
            except OSError as e:
                log.warning("Could not delete the chat backup %s: %s", rel, e)
                failed += 1
                continue
            with ctx.db.tx() as conn:
                conn.execute(
                    "DELETE FROM replica WHERE content_hash = ? AND dest_id = ?", (h, dest_id)
                )
            log.info("Deleted the old chat backup %s on the owner's request", rel)
            deleted += 1
            freed += size
    return {"deleted": deleted, "failed": failed, "freed_bytes": freed}


def as_dict(c: Copy) -> dict[str, Any]:
    return asdict(c)
