"""Find adb, download Google's platform-tools only with consent, and share one adb server.

On macOS only one process can hold a phone's USB interface, so Sift never starts a second
server and never restarts or kills one that is already running on the shared port: it attaches
to whatever answers there and only starts a server when nothing listens on the port at all.
"""

import contextlib
import http.client
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import IO
from urllib.request import urlopen

from sift.core.config import PhoneConfig
from sift.core.paths import AppPaths
from sift.core.proc import Runner, subprocess_runner
from sift.device import adbwire
from sift.device.transport import TransportError

logger = logging.getLogger("sift.device")

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
ADB_SERVER_HOST = "127.0.0.1"

_VERSION_TIMEOUT = 10
_START_SERVER_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 60
_PROBE_DEADLINE_S = 3.0  # the whole probe, connect to last byte
_CHUNK = 1024 * 1024
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_MAX_UNPACKED_BYTES = 500 * 1024 * 1024
_MAX_MEMBERS = 2000

# One download at a time, however many requests arrive.
_download_lock = threading.Lock()

MISSING_MESSAGE = (
    "adb was not found. Install Android platform-tools, or set phone.adb_path in config.toml."
)
PORT_BUSY_MESSAGE = (
    "Another program is using adb's port and is not answering. Close it or press Retry."
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


class AdbPortBusy(Exception):
    """Something listens on adb's port but does not answer like an adb server."""


def adb_server_version(
    host: str, port: int, *, deadline_s: float = _PROBE_DEADLINE_S
) -> str | None:
    """The adb server's protocol version (decimal text) if one answers on host:port.

    Three outcomes:
    - the version, when an adb server answers;
    - None, only when the connection is refused: nothing listens, so starting a server is safe;
    - `AdbPortBusy` when something accepts the connection but does not answer like adb within
      `deadline_s` (one deadline for the whole exchange, not per read): it stays silent,
      trickles, sends something else, or hangs up mid-reply. Starting a server then could
      disturb another program's server, so callers must not run any adb program.

    Speaks the adb host protocol directly over a socket (`adbwire`) and never starts anything.
    """
    deadline = time.monotonic() + deadline_s
    try:
        s = socket.create_connection((host, port), timeout=deadline_s)
    except ConnectionRefusedError:
        return None
    except OSError as e:
        raise AdbPortBusy(f"connecting to {host}:{port} failed: {e!r}") from e
    with s:
        try:
            adbwire.host_request(s, b"host:version", deadline=deadline)
            size = adbwire.read_hex_length(s, max_bytes=16, deadline=deadline)
            if size == 0:
                raise AdbPortBusy("announced a 0-byte version")
            return str(int(adbwire.recv_exact(s, size, deadline=deadline), 16))
        except (TransportError, ValueError) as e:
            raise AdbPortBusy(f"did not answer like adb: {e!r}") from e


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
        server_probe: Callable[[str, int], str | None] = adb_server_version,
    ) -> None:
        """`server_probe` behaves like `adb_server_version`: a version, None when nothing
        listens, or `AdbPortBusy` when something listens but does not answer like adb."""
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
                return _broken(path, configured=True)
            return self._verify(path, configured=True)
        managed = self._paths.platform_tools / "adb"
        if not _is_executable(managed):
            self._restore_previous_install()
        if not _is_executable(managed):
            return AdbLocation(None, None, AdbStatus.missing, MISSING_MESSAGE)
        return self._verify(managed)

    def _restore_previous_install(self) -> None:
        """Put back the copy set aside by an install that was interrupted half way."""
        dest = self._paths.platform_tools
        old = _old_dir(dest)
        if not _is_executable(old / "adb") or self._verify(old / "adb").status is not (
            AdbStatus.ready
        ):
            return
        try:
            if dest.exists():
                dest.rmdir()  # only an empty folder; never remove someone's files
            old.rename(dest)
            logger.warning("Restored the previous platform-tools after an interrupted install")
        except OSError as e:
            logger.warning("Could not restore the previous platform-tools: %s", e)

    def _verify(self, adb: Path, *, configured: bool = False) -> AdbLocation:
        try:
            done = self._runner([str(adb), "version"], _VERSION_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("adb version failed for %s: %s", adb, e)
            return _broken(adb, configured=configured)
        version = _parse_version(done.stdout) if done.returncode == 0 else None
        if version is None:
            logger.warning("adb version gave no version for %s (exit %s)", adb, done.returncode)
            return _broken(adb, configured=configured)
        return AdbLocation(adb, version, AdbStatus.ready, f"adb {version} is ready.")

    # --- shared server --------------------------------------------------------------------

    def ensure_server(self) -> int:
        """Return the adb server port, attaching to a running server or starting one.

        If a server answers, no adb command is run at all, so a server Sift did not start is
        never disturbed. A server is started only when nothing listens on the port (the
        connection is refused); if something listens but does not answer like adb, no adb
        program is run and `AdbServerError` (`PORT_BUSY_MESSAGE`) is raised. `kill-server` is
        never run.
        """
        port = self._config.adb_server_port
        if self._server_answers(port):
            return port
        loc = self.locate()
        if loc.status is not AdbStatus.ready or loc.path is None:
            raise AdbError(loc.message)
        try:
            self._runner([str(loc.path), "-P", str(port), "start-server"], _START_SERVER_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("adb start-server failed: %s", e)
        if not self._server_answers(port):
            raise AdbServerError(
                f"Sift could not start the adb server on port {port}. Another program may be "
                "using that port. Close it and try again, or change phone.adb_server_port "
                "in config.toml."
            )
        return port

    def _server_answers(self, port: int) -> bool:
        """True if an adb server answers, False if nothing listens; else `AdbServerError`."""
        try:
            return self._probe(ADB_SERVER_HOST, port) is not None
        except AdbPortBusy as e:
            logger.warning("Something on adb's port %s does not answer like adb: %s", port, e)
            raise AdbServerError(PORT_BUSY_MESSAGE) from e

    # --- download -------------------------------------------------------------------------

    def download(self, *, consent: bool, opener: Opener = _default_opener) -> AdbLocation:
        """Download Google's platform-tools into `<support>/platform-tools`.

        Raises `ConsentRequired` before any network access unless `consent` is True. The zip is
        checked member by member (no path may leave the target, no links, size caps) and adb
        must run before the new copy replaces an existing one. Downloads run one at a time.
        """
        if not consent:
            raise ConsentRequired(
                "Sift downloads Google's platform-tools only with your permission. "
                "Approve the download to continue."
            )
        with _download_lock:
            try:
                return self._download(opener)
            except (
                zipfile.BadZipFile,
                zlib.error,
                NotImplementedError,
                RuntimeError,
                OSError,
                http.client.IncompleteRead,
            ) as e:
                logger.warning("platform-tools install failed: %r", e)
                raise AdbDownloadError(
                    "The platform-tools package could not be downloaded or installed. "
                    "Check your internet connection and free disk space, then try again."
                ) from e

    def _download(self, opener: Opener) -> AdbLocation:
        support = self._paths.support
        support.mkdir(parents=True, exist_ok=True)
        _sweep_leftovers(support, self._paths.platform_tools)
        archive: Path | None = None
        staging: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".platform-tools-", suffix=".zip", dir=support)
            os.close(fd)
            archive = Path(name)
            staging = Path(tempfile.mkdtemp(prefix=".platform-tools-", suffix=".new", dir=support))
            self._fetch(opener, archive)
            _unpack(archive, staging)
            loc = self._verify(staging / "platform-tools" / "adb")
            if loc.status is not AdbStatus.ready:
                raise AdbDownloadError(
                    "Google's platform-tools downloaded, but adb did not run, so it was not "
                    "installed. Try again, or set phone.adb_path in config.toml."
                )
            _replace_dir(staging / "platform-tools", self._paths.platform_tools)
        finally:
            if archive is not None:
                archive.unlink(missing_ok=True)
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        final = self._paths.platform_tools / "adb"
        return AdbLocation(final, loc.version, AdbStatus.ready, loc.message)

    def _fetch(self, opener: Opener, dest: Path) -> None:
        try:
            with opener(PLATFORM_TOOLS_URL) as src, dest.open("wb") as out:
                total = 0
                while chunk := src.read(_CHUNK):
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise AdbDownloadError(
                            "The platform-tools download is much larger than expected, so "
                            "Sift stopped it. Nothing was installed."
                        )
                    out.write(chunk)
        except (OSError, http.client.IncompleteRead) as e:  # URLError and timeouts are OSErrors
            logger.warning("platform-tools download failed: %r", e)
            raise AdbDownloadError(
                "The platform-tools download did not finish. Check your internet connection "
                "and try again."
            ) from e


def _old_dir(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.old")


def _sweep_leftovers(support: Path, dest: Path) -> None:
    """Remove temp files and folders an earlier, interrupted install left behind."""
    for p in support.glob(".platform-tools-*"):
        if p.suffix in (".zip", ".new"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    if dest.exists():  # otherwise the old copy may be the only working one
        shutil.rmtree(_old_dir(dest), ignore_errors=True)


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _broken(adb: Path, *, configured: bool) -> AdbLocation:
    """A broken adb; the path is shown only when it is the one set in config.toml."""
    which = f"adb at {adb}" if configured else "Sift's copy of adb"
    return AdbLocation(
        adb,
        None,
        AdbStatus.broken,
        f"{which} does not work. Install Android platform-tools, or set phone.adb_path in "
        "config.toml.",
    )


def _bad_zip(why: str) -> AdbDownloadError:
    logger.warning("platform-tools zip rejected: %s", why)
    return AdbDownloadError(
        "The downloaded file was not a valid platform-tools package, so nothing was installed."
    )


def _too_big(why: str) -> AdbDownloadError:
    logger.warning("platform-tools zip rejected: %s", why)
    return AdbDownloadError(
        "The platform-tools package is much larger than expected, so nothing was installed."
    )


def _unpack(archive: Path, staging: Path) -> None:
    """Extract only `platform-tools/...` members into `staging`, refusing anything unsafe."""
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise _bad_zip("not a zip file") from e
    with zf:
        root = staging.resolve()
        members = zf.infolist()
        if len(members) > _MAX_MEMBERS:
            raise _too_big(f"{len(members)} members")
        if sum(i.file_size for i in members) > _MAX_UNPACKED_BYTES:
            raise _too_big("unpacked size too large")
        wanted: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in members:
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
            target.chmod(((perms or 0o644) & 0o755) | 0o600)
        (root / "platform-tools" / "adb").chmod(0o755)


def _replace_dir(new: Path, dest: Path) -> None:
    """Swap `dest` for `new` so a reader sees the old folder or the new one, never half of it."""
    old = _old_dir(dest)
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
