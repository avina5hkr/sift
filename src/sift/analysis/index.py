"""Stage A, Index: what each backed-up file is, read from the Mac copy (never the phone).

For every content with a replica (Mac first, else SSD): mime, width and height (after EXIF
orientation), `has_camera_exif` (EXIF Make or Model), `is_screenshot`, `unreadable` on a decode
error, and a 256 px JPEG thumbnail in the cache folder. Backup files are only opened for
reading. Resumable: a content is done once its `analysis_version` reaches `VERSION`.
"""

import logging
import mimetypes
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from sift.core.db import Database

log = logging.getLogger("sift.analysis")

VERSION = 1
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


def _write_thumb(img: Image.Image, target: Path) -> None:
    thumb = img.copy()
    thumb.thumbnail((THUMB_PX, THUMB_PX))
    if thumb.mode not in ("RGB", "L"):
        thumb = thumb.convert("RGB")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    thumb.save(tmp, "JPEG", quality=82)
    tmp.replace(target)


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
            img = ImageOps.exif_transpose(raw)
            img.load()
            width, height = img.size
            out["width"], out["height"] = width, height
            out["mime"] = Image.MIME.get(raw.format or "", mime)
            _write_thumb(img, thumb_path(cache, digest))
    except Exception as e:  # truncated, corrupt or not an image after all
        log.info("Could not read image %s: %s", path.name, e)
        out["unreadable"] = 1
        return out
    if not out["is_screenshot"] and not out["has_camera_exif"]:
        out["is_screenshot"] = int(screenshot_by_size(width, height, screens))
    return out


def pending(db: Database) -> list[tuple[str, str, str, str]]:
    """(hash, kind, root, rel_path) for contents not indexed yet, the Mac copy first."""
    with db.read() as conn:
        return [
            (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in conn.execute(
                "SELECT c.hash, c.kind, d.root_path, r.rel_path FROM content c "
                "JOIN replica r ON r.content_hash = c.hash JOIN destination d ON d.id = r.dest_id "
                "WHERE c.analysis_version < ? "
                "ORDER BY c.hash, CASE d.kind WHEN 'mac' THEN 0 ELSE 1 END",
                (VERSION,),
            )
        ]


def run_index(
    db: Database,
    cache: Path,
    screens: Sequence[tuple[int, int]] = (),
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[int, int], None] | None = None,
) -> IndexStats:
    """Index every content not indexed yet. Images are decoded; other kinds are recorded as is."""
    stats = IndexStats()
    rows = pending(db)
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
        else:
            cols = {
                "mime": mimetypes.guess_type(path.name, strict=False)[0],
                "unreadable": 0,
                "width": None,
                "height": None,
                "has_camera_exif": 0,
                "is_screenshot": 0,
            }
        stats.indexed += 1
        stats.unreadable += cols["unreadable"] == 1
        with db.tx() as conn:
            conn.execute(
                "UPDATE content SET mime = ?, width = ?, height = ?, has_camera_exif = ?, "
                "is_screenshot = ?, unreadable = ?, analysis_version = ? WHERE hash = ?",
                (
                    cols["mime"],
                    cols["width"],
                    cols["height"],
                    cols["has_camera_exif"],
                    cols["is_screenshot"],
                    cols["unreadable"],
                    VERSION,
                    digest,
                ),
            )
        if on_file is not None:
            on_file(n, len(hashes))
    return stats
