"""Browse and delete (spec 10B): the phone's storage as folders, each file with its backup status.

Everything here reads the database only (the last scan), never the phone. Status per file:
`mac_ssd` (verified on the Mac and the SSD), `mac_only`, `ssd_only`, `not_backed_up`, `changed`
(a present file with a stored hash whose state is not done: plan G2). A quarantined copy never
counts. Folders roll up size, file count, newest mtime and status counts of their files.
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sift.backup.confirm import current_dest_ids, required_kinds
from sift.backup.phonepaths import from_db
from sift.core.config import Config, phone_path_key
from sift.core.db import Database

ROOT = "/sdcard"
DAY = 86_400
_EXT_KIND = {
    "image": {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp"},
    "video": {".mp4", ".mov", ".3gp", ".mkv", ".webm", ".avi"},
    "audio": {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".amr", ".wav", ".flac"},
    "doc": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"},
}


@dataclass
class FileRow:
    id: int
    path: str  # as the phone names it
    size: int
    mtime: int
    origin: str
    kind: str
    status: str
    content_hash: str | None


@dataclass
class Folder:
    name: str
    path: str
    size: int = 0
    files: int = 0
    mtime: int = 0
    status: dict[str, int] = field(default_factory=dict)


def _kind(path: str, stored: str | None) -> str:
    if stored:
        return stored
    ext = PurePosixPath(path).suffix.lower()
    return next((k for k, exts in _EXT_KIND.items() if ext in exts), "other")


def load_rows(db: Database, cfg: Config, device_id: int) -> list[FileRow]:
    dests = current_dest_ids(db, cfg)
    mac, ssd = dests["mac"], dests["ssd"]
    with db.read() as conn:
        held: dict[str, set[int]] = {}
        for digest, dest in conn.execute(
            "SELECT content_hash, dest_id FROM replica WHERE verified_at IS NOT NULL "
            "AND rel_path NOT LIKE '.quarantine/%' AND dest_id IN (?, ?)",
            (mac, ssd),
        ):
            held.setdefault(str(digest), set()).add(int(dest))
        rows = conn.execute(
            "SELECT p.id, p.remote_path, p.size, p.mtime, p.origin, p.state, p.content_hash, "
            "c.kind FROM phone_file p LEFT JOIN content c ON c.hash = p.content_hash "
            "WHERE p.device_id = ? AND p.present = 1",
            (device_id,),
        ).fetchall()
    out = []
    for pid, stored, size, mtime, origin, state, digest, kind in rows:
        path = from_db(str(stored))
        if state == "done" and digest:
            on = held.get(str(digest), set())
            status = (
                "mac_ssd" if mac in on and ssd in on
                else "mac_only" if mac in on
                else "ssd_only" if ssd in on
                else "not_backed_up"
            )  # fmt: skip
        else:
            status = "changed" if digest else "not_backed_up"
        out.append(
            FileRow(int(pid), path, int(size), int(mtime), str(origin), _kind(path, kind),
                    status, str(digest) if digest else None)
        )  # fmt: skip
    return out


def backed_up(cfg: Config, status: str) -> bool:
    """Backed up on every required destination (what a delete needs)."""
    need = required_kinds(cfg)
    if need == ["mac"]:
        return status in ("mac_ssd", "mac_only")
    return status == "mac_ssd"


@dataclass(frozen=True)
class Filters:
    q: str = ""
    kind: str = ""
    older_days: int = 0
    min_bytes: int = 0
    origin: str = ""
    status: str = ""


def matches(f: Filters, row: FileRow, now: float) -> bool:
    if f.q and f.q.casefold() not in PurePosixPath(row.path).name.casefold():
        return False
    if f.kind and row.kind != f.kind:
        return False
    if f.older_days and now - row.mtime < f.older_days * DAY:
        return False
    if f.min_bytes and row.size < f.min_bytes:
        return False
    if f.origin and row.origin != f.origin:
        return False
    return not (f.status and row.status != f.status)


def level(
    rows: Iterable[FileRow], path: str, filters: Filters, now: float | None = None
) -> tuple[list[Folder], list[FileRow]]:
    """The subfolders and files directly inside `path`, after filtering."""
    at = now if now is not None else time.time()
    base = path.rstrip("/") or ROOT
    prefix = phone_path_key(base).rstrip("/") + "/"
    folders: dict[str, Folder] = {}
    files: list[FileRow] = []
    for row in rows:
        key = phone_path_key(row.path)
        if not key.startswith(prefix) or not matches(filters, row, at):
            continue
        rest = row.path[len(base) + 1 :]
        if "/" in rest:
            name = rest.split("/", 1)[0]
            folder = folders.setdefault(name.casefold(), Folder(name, f"{base}/{name}"))
            folder.size += row.size
            folder.files += 1
            folder.mtime = max(folder.mtime, row.mtime)
            folder.status[row.status] = folder.status.get(row.status, 0) + 1
        else:
            files.append(row)
    return list(folders.values()), files


def sort_key(sort: str) -> Callable[[FileRow | Folder], tuple[int, str]]:
    def name(x: FileRow | Folder) -> str:
        return x.name.casefold() if isinstance(x, Folder) else PurePosixPath(x.path).name.casefold()

    if sort == "size":
        return lambda x: (-x.size, name(x))
    if sort == "date":
        return lambda x: (-x.mtime, name(x))
    return lambda x: (0, name(x))


def smart_lists(cfg: Config) -> dict[str, tuple[str, Callable[[FileRow, float], bool]]]:
    b = cfg.browse
    old = b.old_days * DAY

    def under(row: FileRow, *parts: str) -> bool:
        key = phone_path_key(row.path)
        return any(f"/{p.casefold()}/" in key for p in parts)

    return {
        "large_videos": (
            f"Large videos (over {b.large_video_mb} MB)",
            lambda r, now: r.kind == "video" and r.size > b.large_video_mb * 1_000_000,
        ),
        "old_downloads": (
            f"Downloads older than {b.old_days} days",
            lambda r, now: (
                phone_path_key(r.path).startswith("/sdcard/download/") and now - r.mtime > old
            ),
        ),
        "installers": (
            "Installers (.apk, .xapk)",
            lambda r, now: PurePosixPath(r.path).suffix.lower() in (".apk", ".xapk", ".apks"),
        ),
        "old_screenshots": (
            f"Screenshots older than {b.old_days} days",
            lambda r, now: (
                (r.origin == "screenshot" or under(r, "screenshots")) and now - r.mtime > old
            ),
        ),
        "voice_recordings": (
            "Voice recordings",
            lambda r, now: (
                phone_path_key(r.path).startswith("/sdcard/recordings/")
                or under(r, "voice recorder", "sounds")
            ),
        ),
        "unchanged_year": ("Files unchanged for a year", lambda r, now: now - r.mtime > 365 * DAY),
        "whatsapp_media": (
            "WhatsApp voice notes, audio and documents",
            lambda r, now: under(r, "whatsapp voice notes", "whatsapp audio", "whatsapp documents"),
        ),
        "temp_cache": (
            "Temporary and cache-like files",
            lambda r, now: (
                PurePosixPath(r.path).suffix.lower() == ".tmp"
                or under(r, ".thumbnails", ".cache", "cache")
            ),
        ),
    }
