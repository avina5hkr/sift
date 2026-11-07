"""Checks for the Mac backup folder and the phone's folder name, for the first-run wizard.

`check_mac_root` only looks: it never creates, writes or opens anything in the folder. Every
problem comes back as one plain sentence; the operating system's own error text is never shown.
"""

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sift.backup.layout import sanitize_component

DEFAULT_LABEL = "Phone"


@dataclass(frozen=True)
class FolderCheck:
    path: str  # the folder with `~` expanded, as Sift would use it
    ok: bool
    problem: str | None
    exists: bool
    free_bytes: int | None


def _nearest_existing(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def _free(path: Path) -> int | None:
    near = _nearest_existing(path)
    if near is None:
        return None
    try:
        return shutil.disk_usage(near).free
    except OSError:
        return None


def check_mac_root(
    raw: str, *, app_dirs: tuple[Path, ...], home: Path | None = None
) -> FolderCheck:
    """Can `raw` be the Mac backup folder? Needs a full path, a folder (or a place to make
    one) that Sift may write to, and not Sift's own data folders (or a folder holding them)."""
    text = raw.strip()
    home = home if home is not None else Path.home()

    def bad(problem: str, path: str = text, exists: bool = False) -> FolderCheck:
        return FolderCheck(path, False, problem, exists, None)

    if not text:
        return bad("Choose a folder for the backup.")
    if not (text.startswith("/") or text.startswith("~")):
        return bad("Use a full folder path, starting with / or ~ (for example ~/PhoneBackup).")
    expanded = Path(os.path.expanduser(text))
    if not expanded.is_absolute():
        return bad("Use a full folder path, starting with / or ~ (for example ~/PhoneBackup).")
    root = Path(os.path.normpath(expanded))
    shown = str(root)
    try:
        resolved = root.resolve()
        apps = [d.resolve() for d in app_dirs]
        home_resolved = home.resolve()
    except OSError, RuntimeError:
        return bad("Sift cannot read that folder's location. Choose another folder.", shown)
    exists = root.exists()
    if resolved == Path(resolved.anchor) or resolved == home_resolved:
        return bad(
            "Choose a folder just for the backup, not your whole disk or home folder.",
            shown,
            exists,
        )
    if root.suffix.casefold() == ".app":
        return bad(
            "A folder whose name ends in .app looks like an application. Choose another name.",
            shown,
            exists,
        )
    if any(resolved == d or resolved.is_relative_to(d) or d.is_relative_to(resolved) for d in apps):
        return bad("That is where Sift keeps its own data. Choose another folder.", shown, exists)
    if exists and not root.is_dir():
        return bad("That is a file, not a folder. Choose a folder.", shown, True)
    near = _nearest_existing(root)
    if near is None or not near.is_dir() or not os.access(near, os.W_OK | os.X_OK):
        return bad(
            "Sift is not allowed to write there. Choose a folder you can write to.", shown, exists
        )
    return FolderCheck(shown, True, None, exists, _free(root))


def sanitize_label(label: str) -> str:
    """The phone's folder name made safe for any volume; blank becomes "Phone"."""
    text = label.strip() or DEFAULT_LABEL
    return sanitize_component(text, hash8=hashlib.sha256(text.encode()).hexdigest()[:8])
