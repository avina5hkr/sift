"""Where a backed-up file lands under the destination, and the file-name rules.

Pure functions: no database, no transport. Every path component is made safe for APFS, exFAT
and NTFS volumes, so the same backup can sit on any of them:

- a phone name that is not valid UTF-8 (the transport carries those bytes as surrogateescape
  characters) keeps every valid character and writes each bad byte as `%XX`; any other lone
  surrogate becomes U+FFFD. The result is always valid UTF-8 and the same every time;
- the name is NFC;
- `< > : " / \\ | ? *` and control characters (U+0000-U+001F, U+007F-U+009F) become `_`;
- trailing dots and spaces are stripped (leading ones are legal and kept), and a name left
  empty, or made only of dots, becomes `_`, so `.` and `..` can never climb out of the backup;
- Windows device names (CON, NUL, COM1 and the like, with or without an extension) get
  `_HASH8` after the base name;
- a component over 255 UTF-8 bytes (after NFC) is cut at a character boundary so the name still
  ends `_HASH8` plus the original extension, within the limit.

`rel_key` is the case- and normalisation-insensitive form used to detect that two paths would be
the same file on a case-insensitive volume. `resolve_collision` keeps a path when it is free or
already holds the same content, and otherwise adds `_HASH8`.
"""

import hashlib
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PIL import Image

MAX_COMPONENT_BYTES = 255
_MAX_EXT_BYTES = 16  # a longer "extension" is part of the name, not an extension
_FORBIDDEN = frozenset('<>:"/\\|?*')
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"} | {f"{dev}{n}" for dev in ("com", "lpt") for n in "123456789¹²³"}
)
_PHONE_ROOTS = (("sdcard",), ("storage", "emulated", "0"))
_EXIF_IFD = 0x8769
_DATE_TIME_ORIGINAL = 0x9003


def _to_valid_text(name: str) -> str:
    out: list[str] = []
    for ch in name:
        code = ord(ch)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(f"%{code - 0xDC00:02X}")  # a byte the phone name had that is not UTF-8
        elif 0xD800 <= code <= 0xDFFF:
            out.append("�")
        else:
            out.append(ch)
    return "".join(out)


def _is_control(ch: str) -> bool:
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode()) + 1 > _MAX_EXT_BYTES:
        return name, ""
    return stem, "." + ext


def _tagged(name: str, tag: str) -> str:
    """`name` with `_TAG` before its extension, the stem cut so the whole fits 255 bytes."""
    stem, ext = _split_ext(name)
    suffix = f"_{tag}{ext}"
    budget = MAX_COMPONENT_BYTES - len(suffix.encode())
    while len(stem.encode()) > budget:
        stem = stem[:-1]
    out = unicodedata.normalize("NFC", stem.rstrip(". ") + suffix)
    while len(out.encode()) > MAX_COMPONENT_BYTES:  # a rare NFC re-composition at the cut
        stem = stem[:-1]
        out = unicodedata.normalize("NFC", stem.rstrip(". ") + suffix)
    return out


def sanitize_component(name: str, *, hash8: str) -> str:
    """One path component made safe for every volume Sift writes to. See the module notes."""
    text = unicodedata.normalize("NFC", _to_valid_text(name))
    text = "".join("_" if ch in _FORBIDDEN or _is_control(ch) else ch for ch in text)
    text = text.rstrip(". ")
    if not text:
        return "_"
    base, dot, rest = text.partition(".")
    if base.rstrip(" ").casefold() in _RESERVED:
        text = f"{base.rstrip(' ')}_{hash8}{dot}{rest}"
    if len(text.encode()) > MAX_COMPONENT_BYTES:
        text = _tagged(text, hash8)
    return text


def rel_key(rel_path: str) -> str:
    """NFC + casefold: two paths with the same key are one file on a case-insensitive volume."""
    folded = unicodedata.normalize("NFC", rel_path).casefold()
    return unicodedata.normalize("NFC", folded)  # casefold can leave the text un-normalised


def _dir_hash(component: str) -> str:
    """Stable per-folder hash, so a long folder name never depends on the file inside it."""
    return hashlib.sha256(component.encode("utf-8", "surrogateescape")).hexdigest()[:8]


def _phone_components(phone_path: str) -> list[str]:
    parts = [p for p in phone_path.split("/") if p not in ("", ".")]
    for root in _PHONE_ROOTS:
        if tuple(parts[: len(root)]) == root:
            return parts[len(root) :]
    return parts


def _label_and_dirs(device_label: str, dirs: list[str]) -> list[str]:
    return [sanitize_component(c, hash8=_dir_hash(c)) for c in [device_label, *dirs]]


def mirror_rel_path(device_label: str, phone_path: str, *, hash8: str) -> str:
    """`LABEL/<phone path without /sdcard/ or /storage/emulated/0/>`, every part sanitised.

    `hash8` (the file's content hash) is used for the file name; folder names that need
    shortening use a hash of their own name.
    """
    parts = _phone_components(phone_path)
    if not parts:
        raise ValueError(f"phone path names no file: {phone_path!r}")
    head = _label_and_dirs(device_label, parts[:-1])
    return "/".join([*head, sanitize_component(parts[-1], hash8=hash8)])


def by_date_rel_path(device_label: str, phone_path: str, *, taken: datetime, hash8: str) -> str:
    """`LABEL/Camera/YYYY/YYYY-MM/NAME` for the date a photo or video was taken."""
    parts = _phone_components(phone_path)
    if not parts:
        raise ValueError(f"phone path names no file: {phone_path!r}")
    (label,) = _label_and_dirs(device_label, [])
    name = sanitize_component(parts[-1], hash8=hash8)
    return f"{label}/Camera/{taken.year:04d}/{taken.year:04d}-{taken.month:02d}/{name}"


def exif_taken(path: Path) -> datetime | None:
    """EXIF DateTimeOriginal as a naive local time, else None. Never raises."""
    try:
        with Image.open(path) as img:
            raw = img.getexif().get_ifd(_EXIF_IFD).get(_DATE_TIME_ORIGINAL)
        if not isinstance(raw, str):
            return None
        return datetime.strptime(raw.strip().rstrip("\x00"), "%Y:%m:%d %H:%M:%S")
    except Exception:  # unreadable, truncated, not an image, bad date: no date, never an error
        return None


def resolve_collision(
    rel_path: str,
    *,
    hash8: str,
    owner_of: Callable[[str], str | None],
    content_hash: str,
) -> str:
    """The path this content should use: `rel_path` if free or already this content's.

    `owner_of(rel_key)` gives the content hash that owns a path, or None. When another content
    owns `rel_path` (a rename of the same name, or a name differing only by case), `_HASH8` goes
    before the extension. If that is taken too, by other content, `_HASH8_2`, `_HASH8_3` and so
    on are tried, so the answer is always a path this content owns or can have.
    """

    def usable(path: str) -> bool:
        owner = owner_of(rel_key(path))
        return owner is None or owner == content_hash

    if usable(rel_path):
        return rel_path
    parent, slash, name = rel_path.rpartition("/")
    n = 1
    while True:
        tag = hash8 if n == 1 else f"{hash8}_{n}"
        candidate = parent + slash + _tagged(name, tag)
        if usable(candidate):
            return candidate
        n += 1


__all__ = [
    "MAX_COMPONENT_BYTES",
    "by_date_rel_path",
    "exif_taken",
    "mirror_rel_path",
    "rel_key",
    "resolve_collision",
    "sanitize_component",
]
