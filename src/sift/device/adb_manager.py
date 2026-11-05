"""Find adb, download Google's platform-tools only with consent, and share one adb server.

On macOS only one process can hold a phone's USB interface, so Sift never starts a second
server and never restarts or kills one that is already running on the shared port: it attaches
to whatever answers there and only starts a server when nothing does.
"""

import contextlib
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import IO
from urllib.request import urlopen

import adbutils

from sift.core.config import PhoneConfig
from sift.core.paths import AppPaths
from sift.core.proc import Runner, subprocess_runner

logger = logging.getLogger("sift.device")

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
ADB_SERVER_HOST = "127.0.0.1"

_VERSION_TIMEOUT = 10
_START_SERVER_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 60
_PROBE_TIMEOUT = 2.0
_CHUNK = 1024 * 1024

MISSING_MESSAGE = (
    "adb was not found. Sift can download Google's platform-tools for you, "
    "or you can set phone.adb_path in config.toml."
)

Opener = Callable[[str], IO[bytes]]


class AdbStatus(StrEnum):
    ready = "ready"
    missing = "missing"
    broken = "broken"


@dataclass(frozen=True)
class AdbLocation:
    path: Path | None
    version: str | None
    status: AdbStatus
    message: str


class AdbError(Exception):
    """Something about adb went wrong; the message is written for the person using Sift."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConsentRequired(AdbError):
    """The download was asked for without the person's explicit approval."""


class AdbDownloadError(AdbError):
    """The platform-tools download or install did not work."""


class AdbServerError(AdbError):
    """No adb server answers on the shared port and one could not be started."""


def adbutils_server_version(host: str, port: int) -> str | None:
    """The adb server's protocol version if one answers on host:port, else None."""
    # adbutils leaves its socket open when the connection is refused, so first check that
    # something listens at all (and close that probe socket ourselves).
    try:
        socket.create_connection((host, port), timeout=_PROBE_TIMEOUT).close()
    except OSError:
        return None
    try:
        version = adbutils.AdbClient(host=host, port=port, socket_timeout=_PROBE_TIMEOUT)
        return str(version.server_version())
    except adbutils.AdbError, OSError, ValueError:
        return None


def _default_opener(url: str) -> IO[bytes]:
    response: IO[bytes] = urlopen(url, timeout=_DOWNLOAD_TIMEOUT)
    return response


def _parse_version(text: str) -> str | None:
    """The platform-tools version ("35.0.2-123"), else the adb protocol version ("1.0.41")."""
    m = re.search(r"^Version (\S+)", text, re.MULTILINE) or re.search(
        r"^Android Debug Bridge version (\S+)", text, re.MULTILINE
    )
    return m.group(1) if m else None


class AdbManager:
    def __init__(
        self,
        paths: AppPaths,
        config: PhoneConfig,
        *,
        runner: Runner = subprocess_runner,
        server_probe: Callable[[str, int], str | None] = adbutils_server_version,
    ) -> None:
        self._paths = paths
        self._config = config
        self._runner = runner
        self._probe = server_probe

    # --- locating -------------------------------------------------------------------------

    def locate(self) -> AdbLocation:
        """The configured adb, else the managed one, verified by running `adb version`."""
        configured = self._config.adb_path.strip()
        if configured:
            path = Path(configured).expanduser()
            if not _is_executable(path):
                return AdbLocation(
                    path,
                    None,
                    AdbStatus.broken,
                    f"phone.adb_path in config.toml points to {path}, which is not a runnable "
                    "adb. Fix the path, or clear it to use the platform-tools Sift manages.",
                )
            return self._verify(path)
        managed = self._paths.platform_tools / "adb"
        if not _is_executable(managed):
            return AdbLocation(None, None, AdbStatus.missing, MISSING_MESSAGE)
        return self._verify(managed)

    def _verify(self, adb: Path) -> AdbLocation:
        try:
            done = self._runner([str(adb), "version"], _VERSION_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("adb version failed for %s: %s", adb, e)
            return _broken(adb)
        version = _parse_version(done.stdout) if done.returncode == 0 else None
        if version is None:
            logger.warning("adb version gave no version for %s (exit %s)", adb, done.returncode)
            return _broken(adb)
        return AdbLocation(adb, version, AdbStatus.ready, f"adb {version} is ready.")

    # --- shared server --------------------------------------------------------------------

    def ensure_server(self) -> int:
        """Return the adb server port, attaching to a running server or starting one.

        If a server answers, no adb command is run at all, so a server Sift did not start is
        never disturbed. `kill-server` is never run.
        """
        port = self._config.adb_server_port
        if self._probe(ADB_SERVER_HOST, port) is not None:
            return port
        loc = self.locate()
        if loc.status is not AdbStatus.ready or loc.path is None:
            raise AdbError(loc.message)
        try:
            self._runner([str(loc.path), "-P", str(port), "start-server"], _START_SERVER_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("adb start-server failed: %s", e)
        if self._probe(ADB_SERVER_HOST, port) is None:
            raise AdbServerError(
                f"Sift could not start the adb server on port {port}. Another program may be "
                "using that port. Close it and try again, or change phone.adb_server_port "
                "in config.toml."
            )
        return port

    # --- download -------------------------------------------------------------------------

    def download(self, *, consent: bool, opener: Opener = _default_opener) -> AdbLocation:
        """Download Google's platform-tools into `<support>/platform-tools`.

        Raises `ConsentRequired` before any network access unless `consent` is True. The zip is
        checked member by member (no path may leave the target, no links) and adb must run
        before the new copy replaces an existing one.
        """
        if not consent:
            raise ConsentRequired(
                "Sift downloads Google's platform-tools only with your permission. "
                "Approve the download to continue."
            )
        support = self._paths.support
        support.mkdir(parents=True, exist_ok=True)
        archive = Path(tempfile.mkstemp(prefix=".platform-tools-", suffix=".zip", dir=support)[1])
        staging = Path(tempfile.mkdtemp(prefix=".platform-tools-", suffix=".new", dir=support))
        try:
            self._fetch(opener, archive)
            _unpack(archive, staging)
            adb = staging / "platform-tools" / "adb"
            loc = self._verify(adb)
            if loc.status is not AdbStatus.ready:
                raise AdbDownloadError(
                    "Google's platform-tools downloaded, but adb did not run, so it was not "
                    "installed. Try again, or set phone.adb_path in config.toml."
                )
            _replace_dir(staging / "platform-tools", self._paths.platform_tools)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
        final = self._paths.platform_tools / "adb"
        return AdbLocation(final, loc.version, AdbStatus.ready, loc.message)

    def _fetch(self, opener: Opener, dest: Path) -> None:
        try:
            with opener(PLATFORM_TOOLS_URL) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
        except OSError as e:  # URLError and timeouts are OSErrors too
            logger.warning("platform-tools download failed: %s", e)
            raise AdbDownloadError(
                "The platform-tools download did not finish. Check your internet connection "
                "and try again."
            ) from e


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _broken(adb: Path) -> AdbLocation:
    return AdbLocation(
        adb,
        None,
        AdbStatus.broken,
        f"adb at {adb} did not run. Sift can download a fresh copy of Google's platform-tools, "
        "or you can point phone.adb_path in config.toml at a working adb.",
    )


def _bad_zip(why: str) -> AdbDownloadError:
    logger.warning("platform-tools zip rejected: %s", why)
    return AdbDownloadError(
        "The downloaded file was not a valid platform-tools package, so nothing was installed."
    )


def _unpack(archive: Path, staging: Path) -> None:
    """Extract only `platform-tools/...` members into `staging`, refusing anything unsafe."""
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise _bad_zip("not a zip file") from e
    with zf:
        root = staging.resolve()
        wanted: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in zf.infolist():
            name = info.filename
            pure = PurePosixPath(name)
            # Every member is checked, wanted or not: one bad name means a bad archive.
            if pure.is_absolute() or ".." in pure.parts or "\\" in name or "\0" in name:
                raise _bad_zip(f"unsafe member name {name!r}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise _bad_zip(f"link member {name!r}")
            if pure.parts[:1] != ("platform-tools",):
                continue
            target = (root / pure).resolve()
            if not target.is_relative_to(root):
                raise _bad_zip(f"member {name!r} escapes the target")
            wanted.append((info, target))
        if not any(t == root / "platform-tools" / "adb" for _, t in wanted):
            raise _bad_zip("no platform-tools/adb")
        for info, target in wanted:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            perms = (info.external_attr >> 16) & 0o777
            target.chmod((perms or 0o644) | 0o600)
        (root / "platform-tools" / "adb").chmod(0o755)


def _replace_dir(new: Path, dest: Path) -> None:
    """Swap `dest` for `new` so a reader sees the old folder or the new one, never half of it."""
    old = dest.with_name(f".{dest.name}.old")
    shutil.rmtree(old, ignore_errors=True)
    had_old = dest.exists()
    if had_old:
        dest.rename(old)
    try:
        new.rename(dest)
    except OSError:
        if had_old:
            with contextlib.suppress(OSError):
                old.rename(dest)
        raise
    shutil.rmtree(old, ignore_errors=True)
