"""Writing a new file into a backup destination without ever replacing one (I1)."""

import os
import time
from pathlib import Path, PurePosixPath


def write_new(path: Path, body: bytes) -> Path:
    """Write `body` to a temp file, fsync it, then move it into place under `path`, or
    `stem-N.suffix` when that name is taken. Returns where it went; never replaces a file."""
    tmp = path.with_name(f".{path.name}.part-{os.getpid()}-{time.monotonic_ns()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        return place_new(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def place_new(src: Path, path: Path) -> Path:
    """Move `src` (same folder) to `path` or the first free `stem-N.suffix`; never replaces a
    file. Uses the backup's no-replace rename, which also works on exFAT (no hard links)."""
    from sift.backup.staging import TargetExists, _rename_no_replace

    stem, suffix, n = path.stem, path.suffix, 1
    while True:
        try:
            _rename_no_replace(src, path)
            return path
        except TargetExists:
            path = path.with_name(f"{stem}-{n}{suffix}")
            n += 1


def report_folder(root: Path, report_dir: str) -> Path:
    """`root/report_dir`, or `root/.sift/reports` when the setting would leave the root."""
    rel = PurePosixPath(report_dir)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        rel = PurePosixPath(".sift/reports")
    return root / rel


def write_to_each(
    roots: list[Path], report_dir: str, name: str, body: bytes
) -> tuple[list[str], list[str]]:
    """`write_new` into `report_folder(root, report_dir)/name` on every root; returns the paths
    written and one line per root that failed (a drive that refuses does not stop the rest)."""
    written, failed = [], []
    for root in roots:
        try:
            folder = report_folder(root, report_dir)
            folder.mkdir(parents=True, exist_ok=True)
            written.append(str(write_new(folder / name, body)))
        except OSError as e:
            failed.append(f"{root}: {e.strerror or e}")
    return written, failed
