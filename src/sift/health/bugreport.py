"""Capture a bug report (`bugreportz`) and save the zip to `DEST_ROOT/.sift/reports/`."""

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from sift.core.files import place_new, report_folder
from sift.device.transport import Transport


class BugReportStopped(Exception):
    """The owner pressed Stop while the zip was being pulled."""


def capture(
    transport: Transport,
    roots: list[Path],
    report_dir: str,
    *,
    on_progress: Callable[[int, int], None],
    should_stop: Callable[[], bool],
) -> list[str]:
    """Run the capture on the phone, pull the zip once, and place a copy on every root
    (temp name, then moved into place; never replaces a file). Returns the saved paths."""
    if not roots:
        raise ValueError("Connect a backup folder first.")
    remote = transport.capture_bugreport(on_progress, should_stop)
    name = PurePosixPath(remote).name or "bugreport.zip"
    if not name.endswith(".zip"):
        name += ".zip"
    saved: list[str] = []
    failed: list[str] = []
    source: Path | None = None
    for root in roots:  # pull once, onto the first folder that takes it
        try:
            folder = report_folder(root, report_dir)
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{name}.part-{os.getpid()}-{time.monotonic_ns()}"
            try:
                if source is None:
                    with transport.open_read(remote) as src, open(tmp, "xb") as out:
                        while chunk := src.read(1 << 20):
                            if should_stop():
                                raise BugReportStopped("Stopped.")
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                else:
                    shutil.copyfile(source, tmp)
                placed = place_new(tmp, folder / name)
            finally:
                tmp.unlink(missing_ok=True)
            source = source or placed
            saved.append(str(placed))
        except OSError as e:
            failed.append(f"{root}: {e}")
    if not saved:
        raise OSError("The bug report could not be saved: " + "; ".join(failed))
    return saved
