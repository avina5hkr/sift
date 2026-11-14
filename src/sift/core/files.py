"""Writing a new file into a backup destination without ever replacing one (I1)."""

import os
import time
from pathlib import Path, PurePosixPath


def write_new(path: Path, body: bytes) -> Path:
    """Write `body` to a temp file, fsync it, then link it into place under `path`, or
    `stem-N.suffix` when that name is taken. Returns where it went; never replaces a file."""
    tmp = path.with_name(f".{path.name}.part-{os.getpid()}-{time.monotonic_ns()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        return link_new(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def link_new(src: Path, path: Path) -> Path:
    """Hard-link `src` to `path` or the first free `stem-N.suffix`; never replaces a file."""
    stem, suffix, n = path.stem, path.suffix, 1
    while True:
        try:
            os.link(src, path)
            return path
        except FileExistsError:
            path = path.with_name(f"{stem}-{n}{suffix}")
            n += 1


def report_folder(root: Path, report_dir: str) -> Path:
    """`root/report_dir`, or `root/.sift/reports` when the setting would leave the root."""
    rel = PurePosixPath(report_dir)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        rel = PurePosixPath(".sift/reports")
    return root / rel
