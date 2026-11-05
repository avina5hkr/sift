"""Stop adbutils from ever launching an adb command line program.

adbutils' `AdbConnection._safe_connect` runs `adb start-server` whenever a connect fails or
times out, using whichever adb it finds first (`$ADBUTILS_ADB_PATH`, one on PATH, or the copy
bundled in its wheel). An adb of a different version kills and restarts the adb server that is
already running, which would drop the phone's connection for whoever else is using it. Sift
starts a server itself, once and deliberately (`AdbManager.ensure_server`), so every part of
Sift that uses adbutils calls `install_no_spawn_guard()` first.
"""

from typing import Any

import adbutils
from adbutils import _adb

PINNED_ADBUTILS_VERSION = "2.12.0"


def _connect_only(self: Any) -> Any:
    """Connect to the adb server; on failure let adbutils' own connection errors propagate."""
    return self._create_socket()


def install_no_spawn_guard() -> None:
    """Replace adbutils' connect-and-maybe-spawn with a connect-only version. Idempotent."""
    if adbutils.__version__ != PINNED_ADBUTILS_VERSION:
        raise RuntimeError(
            f"Sift is built for adbutils {PINNED_ADBUTILS_VERSION} but found "
            f"{adbutils.__version__}. Its internals changed, so the guard that stops it from "
            "starting adb on its own must be checked before this version is used."
        )
    connection = getattr(_adb, "AdbConnection", None)
    if (
        connection is None
        or not hasattr(connection, "_safe_connect")
        or not hasattr(connection, "_create_socket")
    ):
        raise RuntimeError(
            "adbutils no longer has the connection hooks Sift guards "
            "(AdbConnection._safe_connect / _create_socket). Update adbutils_guard.py."
        )
    connection._safe_connect = _connect_only
