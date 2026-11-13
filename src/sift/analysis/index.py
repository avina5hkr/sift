"""Stage A, Index: what each backed-up file is, read from the Mac copy (never the phone).

For every content with a replica (Mac first, else SSD): mime, width and height (after EXIF
orientation), `has_camera_exif` (EXIF Make or Model), `is_screenshot`, `unreadable` on a decode
error, and a 256 px JPEG thumbnail in the cache folder. Backup files are only opened for
reading. Resumable: a content is done once its `analysis_version` reaches `VERSION`.
"""

import json
import logging
import mimetypes
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from sift.analysis.phash import hashes
from sift.analysis.video import probe
from sift.core.db import Database

log = logging.getLogger("sift.analysis")

VERSION = 2  # 1: index (stage A); 2: perceptual hashes and video fingerprints (C, E)
THUMB_PX = 256
Image.MAX_IMAGE_PIXELS = 400_000_000  # big panoramas are real photos, not decompression bombs

try:  # HEIC/HEIF from phone cameras
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - the dependency is installed; stay usable without it
    log.warning("HEIC support is not installed; HEIC photos are treated as unreadable")

_EXIF_MAKE, _EXIF_MODEL = 0x010F, 0x0110
_SCREENSHOT_NAME = re.compile(r"^screenshot[_ -]", re.IGNORECASE)


@dataclass
class IndexStats:
    indexed: int = 0
    unreadable: int = 0
    skipped: int = 0  # no plugged-in copy to read


def thumb_path(cache: Path, digest: str) -> Path:
    return cache / "thumbs" / digest[:2] / f"{digest}.jpg"


def screenshot_by_path(phone_paths: Sequence[str]) -> bool:
    """The path and name rules come first (spec 8): a Screenshots folder or a Screenshot_ name."""
    for path in phone_paths:
        parts = path.casefold().split("/")
        if "screenshots" in parts[:-1] or _SCREENSHOT_NAME.match(path.rsplit("/", 1)[-1]):
            return True
    return False


def screenshot_by_size(width: int, height: int, screens: Sequence[tuple[int, int]]) -> bool:
    return any({width, height} == {w, h} for w, h in screens)


_RENDERS = threading.BoundedSemaphore(2)  # big images decode at most two at a time
_ROTATED = {5, 6, 7, 8}  # EXIF orientations that swap width and height


def render_jpeg(
    source: Path, target: Path, size: int, *, with_hashes: bool = False
) -> tuple[int, int, str | None, tuple[int, int] | None]:
    """Write a JPEG of `source` at most `size` px on its long edge to `target` (in the cache).

    Returns the image's true width and height (after EXIF orientation), its format and, when
    asked, its (dHash, pHash) taken from the oriented, reduced image. JPEGs
    are decoded at a reduced scale (draft mode), so a 100-megapixel photo costs a few MB. The
    file is written under a unique temporary name and renamed, so concurrent renders of the same
    content never collide. `source` is only read.
    """
    with _RENDERS, Image.open(source) as raw:
        width, height = raw.size
        orientation = raw.getexif().get(0x0112, 1)
        if orientation in _ROTATED:
            width, height = height, width
        fmt = raw.format
        if fmt == "JPEG":
            raw.draft("RGB", (size, size))
        img = ImageOps.exif_transpose(raw)
        img.thumbnail((size, size))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        found = hashes(img) if with_hashes else None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            img.save(tmp, "JPEG", quality=82)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
    return width, height, fmt, found


def index_file(
    path: Path,
    digest: str,
    phone_paths: Sequence[str],
    screens: Sequence[tuple[int, int]],
    cache: Path,
) -> dict[str, object]:
    """The `content` columns for one file. Never raises for a bad file: it is `unreadable`."""
    mime, _ = mimetypes.guess_type(path.name, strict=False)
    out: dict[str, object] = {
        "mime": mime,
        "unreadable": 0,
        "width": None,
        "height": None,
        "has_camera_exif": 0,
        "is_screenshot": int(screenshot_by_path(phone_paths)),
    }
    try:
        with Image.open(path) as raw:
            exif = raw.getexif()
            out["has_camera_exif"] = int(bool(exif.get(_EXIF_MAKE) or exif.get(_EXIF_MODEL)))
        width, height, fmt, found = render_jpeg(
            path, thumb_path(cache, digest), THUMB_PX, with_hashes=True
        )
        out["width"], out["height"] = width, height
        if found is not None:
            out["dhash"], out["phash"] = found
        out["mime"] = Image.MIME.get(fmt or "", mime)
    except Exception as e:  # truncated, corrupt or not an image after all
        log.info("Could not read image %s: %s", path.name, e)
        out["unreadable"] = 1
        return out
    if not out["is_screenshot"] and not out["has_camera_exif"]:
        out["is_screenshot"] = int(screenshot_by_size(width, height, screens))
    return out


_EMPTY: dict[str, object] = {
    "mime": None,
    "width": None,
    "height": None,
    "has_camera_exif": 0,
    "is_screenshot": 0,
    "unreadable": 0,
    "dhash": None,
    "phash": None,
    "duration_ms": None,
    "vframes": None,
}


def index_video(path: Path, digest: str, cache: Path) -> dict[str, object]:
    """Duration, size and the five-frame fingerprint; the 50 % frame becomes the thumbnail."""
    mime = mimetypes.guess_type(path.name, strict=False)[0]
    info = probe(path)
    if info is None:
        return {"mime": mime, "unreadable": 1}
    if info.poster is not None:
        target = thumb_path(cache, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        thumb = info.poster.copy()
        thumb.thumbnail((THUMB_PX, THUMB_PX))
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            thumb.convert("RGB").save(tmp, "JPEG", quality=82)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
    return {
        "mime": mime,
        "width": info.width,
        "height": info.height,
        "duration_ms": info.duration_ms,
        "vframes": json.dumps(list(info.frames)),
        "dhash": info.frames[len(info.frames) // 2],  # the middle frame stands for the video
    }


def pending(db: Database, dest_ids: set[int] | None = None) -> list[tuple[str, str, str, str]]:
    """(hash, kind, root, rel_path) for contents not indexed yet, the Mac copy first."""
    with db.read() as conn:
        return [
            (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in conn.execute(
                "SELECT c.hash, c.kind, d.root_path, r.rel_path, d.id FROM content c "
                "JOIN replica r ON r.content_hash = c.hash JOIN destination d ON d.id = r.dest_id "
                "WHERE c.analysis_version < ? "
                "ORDER BY c.hash, CASE d.kind WHEN 'mac' THEN 0 ELSE 1 END",
                (VERSION,),
            )
            if dest_ids is None or int(r[4]) in dest_ids  # only drives found by UUID now
        ]


def run_index(
    db: Database,
    cache: Path,
    screens: Sequence[tuple[int, int]] = (),
    *,
    dest_ids: set[int] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[int, int], None] | None = None,
) -> IndexStats:
    """Index every content not indexed yet. Images are decoded; other kinds are recorded as is."""
    stats = IndexStats()
    rows = pending(db, dest_ids)
    hashes = list(dict.fromkeys(r[0] for r in rows))
    copies: dict[str, list[Path]] = {}
    kinds: dict[str, str] = {}
    for digest, kind, root, rel in rows:
        copies.setdefault(digest, []).append(Path(root).expanduser() / rel)
        kinds[digest] = kind
    for n, digest in enumerate(hashes, 1):
        if should_stop():
            break
        path = next((p for p in copies[digest] if p.is_file()), None)
        if path is None:
            stats.skipped += 1
            continue
        with db.read() as conn:
            phone_paths = [
                str(r[0])
                for r in conn.execute(
                    "SELECT remote_path FROM phone_file WHERE content_hash = ?", (digest,)
                )
            ]
        if kinds[digest] == "image":
            cols = index_file(path, digest, phone_paths, screens, cache)
        elif kinds[digest] == "video":
            cols = index_video(path, digest, cache)
        else:
            cols = {"mime": mimetypes.guess_type(path.name, strict=False)[0], "unreadable": 0}
        stats.indexed += 1
        stats.unreadable += cols.get("unreadable") == 1
        values = {**_EMPTY, **cols}
        with db.tx() as conn:
            conn.execute(
                "UPDATE content SET mime = ?, width = ?, height = ?, has_camera_exif = ?, "
                "is_screenshot = ?, unreadable = ?, dhash = ?, phash = ?, duration_ms = ?, "
                "vframes = ?, analysis_version = ? WHERE hash = ?",
                (
                    values["mime"],
                    values["width"],
                    values["height"],
                    values["has_camera_exif"],
                    values["is_screenshot"],
                    values["unreadable"],
                    values["dhash"],
                    values["phash"],
                    values["duration_ms"],
                    values["vframes"],
                    VERSION,
                    digest,
                ),
            )
        if on_file is not None:
            on_file(n, len(hashes))
    return stats
