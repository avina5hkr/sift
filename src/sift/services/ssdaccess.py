"""May Sift write to the chosen SSD? A small, safe probe (plan A4).

macOS asks once whether an app may use removable volumes. Until the owner answers, a write to
the drive waits inside the system; when they say no, it fails with EPERM. The probe tells these
apart from a full or read-only drive, so Setup and Destinations can say what to do.

What the probe touches, and nothing else:

1. it records a unique name `.sift-access-<random>` (in the `setting` table) before writing;
2. it creates that folder at the top of the drive with an exclusive `mkdir`, and one file
   inside it with an exclusive create, writes a few bytes and flushes;
3. it removes only those two, the file and then the folder, and clears the record.

A probe interrupted by a quit is finished at the next start from the recorded name: the file
named `probe` inside that folder, then the folder if it is then empty. Nothing else is removed.
"""

import contextlib
import errno
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sift.core.db import Database

log = logging.getLogger("sift.ssd")

PROBE_KEY = "ssd_access_probe"
PREFIX = ".sift-access-"
FILE = "probe"
WAIT_S = 15.0  # how long a request waits for the probe before answering "waiting"

_probe_lock = threading.Lock()
_probing: threading.Thread | None = None

MESSAGES = {
    "ok": "Sift can write to this drive.",
    "denied": (
        "macOS has not allowed Sift to use this drive. Open System Settings, Privacy & "
        "Security, Files and Folders, and turn on Removable Volumes for Sift."
    ),
    "full": "The drive is full. Make room on it, or choose another drive.",
    "read_only": "The drive is read-only, so Sift cannot copy to it. Choose another drive.",
    "not_connected": "The SSD is not connected. Plug it in and check again.",
    "other_drive": "A different drive is connected under this name. Plug in the chosen SSD.",
    "waiting": (
        "Waiting for macOS: check for a permission prompt and choose Allow, then check again."
    ),
    "error": "Sift could not write to this drive. Check that it works in Finder, then try again.",
}


@dataclass(frozen=True)
class AccessResult:
    state: str  # a key of MESSAGES
    message: str

    @classmethod
    def of(cls, state: str) -> AccessResult:
        return cls(state, MESSAGES[state])


def _classify(e: OSError) -> str:
    if e.errno in (errno.EPERM, errno.EACCES):
        return "denied"
    if e.errno == errno.ENOSPC:
        return "full"
    if e.errno == errno.EROFS:
        return "read_only"
    return "error"


def _record(db: Database, folder: Path | None) -> None:
    with db.tx() as conn:
        if folder is None:
            conn.execute("DELETE FROM setting WHERE key = ?", (PROBE_KEY,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)",
                (PROBE_KEY, json.dumps({"folder": str(folder)})),
            )


def _remove(folder: Path, *, made_file: bool, made_folder: bool) -> None:
    if made_file:
        (folder / FILE).unlink(missing_ok=True)
    if made_folder:
        with contextlib.suppress(FileNotFoundError):
            folder.rmdir()


def finish_leftover(db: Database) -> bool:
    """Finish a probe a quit interrupted: remove its file and folder (only those). True when
    nothing is left to do."""
    with db.read() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (PROBE_KEY,)).fetchone()
    if row is None:
        return True
    try:
        folder = Path(json.loads(row[0])["folder"])
    except ValueError, KeyError, TypeError:
        _record(db, None)
        return True
    if not folder.name.startswith(PREFIX):
        _record(db, None)  # never ours: leave the drive alone
        return True
    if not folder.parent.is_dir():
        return False  # the drive is not plugged in: try when it is
    try:
        (folder / FILE).unlink(missing_ok=True)
        if folder.is_dir():
            folder.rmdir()
    except OSError as e:
        log.warning("Could not remove the access check's folder from the SSD: %s", e)
        return False
    _record(db, None)
    return True


def probe(db: Database, mount: Path) -> AccessResult:
    """Try one write at the top of the drive mounted at `mount`, then remove it."""
    finish_leftover(db)
    if not os.path.ismount(mount) and not mount.is_dir():
        return AccessResult.of("not_connected")
    folder = mount / f"{PREFIX}{secrets.token_hex(8)}"
    _record(db, folder)
    made_folder = made_file = False
    try:
        os.mkdir(folder, 0o700)  # exclusive: fails if anything is there
        made_folder = True
        fd = os.open(folder / FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        made_file = True
        try:
            os.write(fd, b"sift")
            os.fsync(fd)
        finally:
            os.close(fd)
        result = AccessResult.of("ok")
    except OSError as e:
        result = AccessResult.of(_classify(e))
        log.info("SSD access check: %s (%s)", result.state, errno.errorcode.get(e.errno or 0))
    try:
        _remove(folder, made_file=made_file, made_folder=made_folder)
        _record(db, None)
    except OSError as e:
        log.warning("Could not remove the access check's folder from the SSD: %s", e)
    return result


def probe_waiting(
    db: Database, mount: Path, *, wait_s: float = WAIT_S, run: Callable[..., AccessResult] = probe
) -> AccessResult:
    """`probe` on its own thread; "waiting" when it has not finished in `wait_s` (a macOS prompt
    is open). The thread finishes and cleans up by itself when the owner answers."""
    global _probing
    with _probe_lock:
        if _probing is not None and _probing.is_alive():
            return AccessResult.of("waiting")  # one probe at a time: the first is still held
        box: list[AccessResult] = []

        def work() -> None:
            try:
                box.append(run(db, mount))
            except Exception:
                log.exception("SSD access check failed")
                box.append(AccessResult.of("error"))
            finally:
                db.release_thread()

        t = threading.Thread(target=work, name="sift-ssd-access", daemon=True)
        _probing = t
        t.start()
    t.join(wait_s)
    return box[0] if box else AccessResult.of("waiting")
