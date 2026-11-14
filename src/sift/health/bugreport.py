"""Capture a bug report (`bugreportz`) and save the zip to `DEST_ROOT/.sift/reports/`."""

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from sift.core.files import link_new, report_folder
from sift.device.transport import Transport


def capture(
    transport: Transport,
    roots: list[Path],
    report_dir: str,
    *,
    on_progress: Callable[[int, int], None],
    should_stop: Callable[[], bool],
) -> list[str]:
    """Run the capture on the phone, pull the zip once, and place a copy on every root
    (temp name, then linked into place; never replaces a file). Returns the saved paths."""
    if not roots:
        raise ValueError("Connect a backup folder first.")
    remote = transport.capture_bugreport(on_progress, should_stop)
    name = PurePosixPath(remote).name or "bugreport.zip"
    if not name.endswith(".zip"):
        name += ".zip"
    first = report_folder(roots[0], report_dir)
    first.mkdir(parents=True, exist_ok=True)
    tmp = first / f".{name}.part-{os.getpid()}-{time.monotonic_ns()}"
    saved: list[str] = []
    try:
        with transport.open_read(remote) as src, open(tmp, "xb") as out:
            while chunk := src.read(1 << 20):
                if should_stop():
                    raise InterruptedError("Stopped.")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        saved.append(str(link_new(tmp, first / name)))
        for root in roots[1:]:
            folder = report_folder(root, report_dir)
            folder.mkdir(parents=True, exist_ok=True)
            other = folder / f".{name}.part-{os.getpid()}-{time.monotonic_ns()}"
            try:
                shutil.copyfile(tmp, other)
                saved.append(str(link_new(other, folder / name)))
            except OSError:
                pass
            finally:
                other.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)
    return saved
