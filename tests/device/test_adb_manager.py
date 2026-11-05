import contextlib
import functools
import http.client
import io
import os
import socket
import stat
import subprocess
import threading
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from sift.core.config import PhoneConfig
from sift.core.paths import AppPaths
from sift.device.adb_manager import (
    PLATFORM_TOOLS_URL,
    AdbDownloadError,
    AdbError,
    AdbLocation,
    AdbManager,
    AdbPortBusy,
    AdbServerError,
    AdbStatus,
    ConsentRequired,
    adb_server_version,
)
from sift.server.routes.devices import get_adb_manager
from tests.helpers import H, client_for, make_ctx

VERSION_OUT = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 35.0.2-12147458\n"
    "Installed as /somewhere/adb\n"
    "Running on Darwin 25.0.0 (arm64)\n"
)


class FakeRunner:
    """Records every call; answers `version` and `start-server` unless told otherwise."""

    def __init__(self, *, version_rc: int = 0, start_rc: int = 0) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.version_rc = version_rc
        self.start_rc = start_rc

    def __call__(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), timeout))
        if "version" in args:
            rc = self.version_rc
            return subprocess.CompletedProcess(list(args), rc, VERSION_OUT if rc == 0 else "", "")
        return subprocess.CompletedProcess(list(args), self.start_rc, "", "")


def never_probe(host: str, port: int) -> str | None:
    raise AssertionError("the server must not be probed here")


def make_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def manager(paths: AppPaths, runner=None, probe=never_probe, **phone) -> AdbManager:
    return AdbManager(
        paths, PhoneConfig(**phone), runner=runner or FakeRunner(), server_probe=probe
    )


def build_zip(members: dict[str, bytes], *, symlinks: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o644) << 16  # not executable on purpose
            zf.writestr(info, data)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


GOOD_ZIP = build_zip({"platform-tools/adb": b"#!/bin/sh\n", "platform-tools/NOTICE.txt": b"n"})


class Opener:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.urls: list[str] = []

    def __call__(self, url: str) -> io.BytesIO:
        self.urls.append(url)
        return io.BytesIO(self.data)


# --- locate ---------------------------------------------------------------------------------


def test_locate_prefers_configured_path(paths, tmp_path):
    mine = make_exe(tmp_path / "mine" / "adb")
    make_exe(paths.platform_tools / "adb")
    runner = FakeRunner()
    loc = manager(paths, runner, adb_path=str(mine)).locate()
    assert (loc.status, loc.path, loc.version) == (AdbStatus.ready, mine, "35.0.2-12147458")
    assert runner.calls == [([str(mine), "version"], 10)]


def test_locate_uses_managed_platform_tools(paths):
    adb = make_exe(paths.platform_tools / "adb")
    loc = manager(paths).locate()
    assert (loc.status, loc.path) == (AdbStatus.ready, adb)


def test_locate_reports_missing_in_plain_words(paths):
    loc = manager(paths).locate()
    assert (loc.status, loc.path, loc.version) == (AdbStatus.missing, None, None)
    assert loc.message == (
        "adb was not found. Install Android platform-tools, or set phone.adb_path in config.toml."
    )


def test_locate_configured_path_that_does_not_exist_is_broken(paths, tmp_path):
    make_exe(paths.platform_tools / "adb")  # a managed copy must not silently replace the choice
    loc = manager(paths, adb_path=str(tmp_path / "nope")).locate()
    assert loc.status == AdbStatus.broken and "phone.adb_path" in loc.message


def test_locate_configured_path_not_executable_is_broken(paths, tmp_path):
    p = tmp_path / "adb"
    p.write_text("x")
    p.chmod(0o644)
    loc = manager(paths, adb_path=str(p)).locate()
    assert loc.status == AdbStatus.broken and "phone.adb_path" in loc.message


def test_locate_broken_when_adb_version_fails(paths):
    make_exe(paths.platform_tools / "adb")
    loc = manager(paths, FakeRunner(version_rc=1)).locate()
    assert loc.status == AdbStatus.broken and loc.version is None
    assert "did not run" in loc.message


def test_locate_broken_when_adb_cannot_start(paths):
    make_exe(paths.platform_tools / "adb")

    def boom(args, timeout):
        raise OSError("Exec format error")

    assert manager(paths, boom).locate().status == AdbStatus.broken


def test_locate_broken_when_adb_hangs(paths):
    make_exe(paths.platform_tools / "adb")

    def hang(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    assert manager(paths, hang).locate().status == AdbStatus.broken


# --- download -------------------------------------------------------------------------------


def test_download_requires_consent(paths):
    opener = Opener(GOOD_ZIP)
    with pytest.raises(ConsentRequired):
        manager(paths).download(consent=False, opener=opener)
    assert opener.urls == []
    assert not (paths.platform_tools / "adb").exists()


def test_download_installs_and_verifies(paths):
    opener = Opener(GOOD_ZIP)
    runner = FakeRunner()
    loc = manager(paths, runner).download(consent=True, opener=opener)
    adb = paths.platform_tools / "adb"
    assert opener.urls == [PLATFORM_TOOLS_URL]
    assert PLATFORM_TOOLS_URL == (
        "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
    )
    assert loc == AdbLocation(adb, "35.0.2-12147458", AdbStatus.ready, loc.message)
    assert adb.read_bytes() == b"#!/bin/sh\n" and os.access(adb, os.X_OK)
    assert (paths.platform_tools / "NOTICE.txt").read_bytes() == b"n"
    assert [c[0][1:] for c in runner.calls] == [["version"]]  # verified once, before installing
    # No leftovers next to the install.
    assert sorted(
        p.name for p in paths.support.iterdir() if "platform" in p.name or ".tmp" in p.name
    ) == ["platform-tools"]


def test_download_replaces_an_existing_install(paths):
    make_exe(paths.platform_tools / "adb")
    (paths.platform_tools / "stale.txt").write_text("old")
    manager(paths).download(consent=True, opener=Opener(GOOD_ZIP))
    assert not (paths.platform_tools / "stale.txt").exists()
    assert (paths.platform_tools / "adb").read_bytes() == b"#!/bin/sh\n"


def test_download_ignores_members_outside_platform_tools(paths):
    data = build_zip({"platform-tools/adb": b"a", "other/thing": b"x"})
    manager(paths).download(consent=True, opener=Opener(data))
    assert not (paths.support / "other").exists()
    assert not (paths.platform_tools / "other").exists()


@pytest.mark.parametrize(
    "name",
    [
        "platform-tools/../../escape.txt",
        "platform-tools/sub/../../../escape.txt",
        "/abs/escape.txt",
        "../escape.txt",
    ],
)
def test_download_rejects_zip_slip(paths, tmp_path, name):
    make_exe(paths.platform_tools / "adb")
    (paths.platform_tools / "keep.txt").write_text("keep")
    data = build_zip({"platform-tools/adb": b"a", name: b"evil"})
    with pytest.raises(AdbDownloadError):
        manager(paths).download(consent=True, opener=Opener(data))
    assert not list(tmp_path.rglob("escape.txt"))
    assert (paths.platform_tools / "keep.txt").read_text() == "keep"  # old install untouched


def test_download_rejects_symlink_members(paths):
    data = build_zip({"platform-tools/adb": b"a"}, symlinks=("platform-tools/link",))
    with pytest.raises(AdbDownloadError):
        manager(paths).download(consent=True, opener=Opener(data))


def test_download_rejects_zip_without_adb(paths):
    data = build_zip({"platform-tools/fastboot": b"f"})
    with pytest.raises(AdbDownloadError, match="not a valid platform-tools package"):
        manager(paths).download(consent=True, opener=Opener(data))


def test_download_rejects_a_file_that_is_not_a_zip(paths):
    with pytest.raises(AdbDownloadError):
        manager(paths).download(consent=True, opener=Opener(b"<html>nope</html>"))


def test_download_network_failure_is_a_plain_error(paths):
    def offline(url: str):
        raise OSError("Network is unreachable")

    with pytest.raises(AdbDownloadError, match="download"):
        manager(paths).download(consent=True, opener=offline)


def test_download_that_fails_verification_keeps_the_old_install(paths):
    make_exe(paths.platform_tools / "adb")
    (paths.platform_tools / "keep.txt").write_text("keep")
    with pytest.raises(AdbDownloadError):
        manager(paths, FakeRunner(version_rc=1)).download(consent=True, opener=Opener(GOOD_ZIP))
    assert (paths.platform_tools / "keep.txt").read_text() == "keep"
    assert [p.name for p in paths.support.iterdir() if p.name.startswith(".")] == []


# --- download hardening -------------------------------------------------------------------------


def test_download_stream_cap(paths, monkeypatch):
    monkeypatch.setattr("sift.device.adb_manager._MAX_DOWNLOAD_BYTES", 100)
    with pytest.raises(AdbDownloadError, match="larger than expected"):
        manager(paths).download(consent=True, opener=Opener(GOOD_ZIP + b"x" * 1000))
    assert not (paths.platform_tools / "adb").exists()


def test_download_unpacked_size_cap(paths, monkeypatch):
    monkeypatch.setattr("sift.device.adb_manager._MAX_UNPACKED_BYTES", 5)
    data = build_zip({"platform-tools/adb": b"0123456789"})
    with pytest.raises(AdbDownloadError, match="larger than expected"):
        manager(paths).download(consent=True, opener=Opener(data))


def test_download_member_count_cap(paths, monkeypatch):
    monkeypatch.setattr("sift.device.adb_manager._MAX_MEMBERS", 3)
    data = build_zip(
        {"platform-tools/adb": b"a", **{f"platform-tools/f{i}": b"x" for i in range(5)}}
    )
    with pytest.raises(AdbDownloadError, match="larger than expected"):
        manager(paths).download(consent=True, opener=Opener(data))


def _corrupt(data: bytes, compression: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        zf.writestr("platform-tools/adb", b"A" * 5000 if compression else b"abcdefgh")
    raw = bytearray(buf.getvalue())
    start = 30 + len("platform-tools/adb")
    raw[start : start + 8] = b"\xff" * 8
    return bytes(raw)


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_download_corrupt_member_is_a_plain_error(paths, compression):
    make_exe(paths.platform_tools / "adb")
    with pytest.raises(AdbDownloadError, match="could not be downloaded or installed"):
        manager(paths).download(consent=True, opener=Opener(_corrupt(b"", compression)))
    assert [p.name for p in paths.support.iterdir() if p.name.startswith(".")] == []


class TruncatedStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        data = super().read(size)
        if not data:
            raise http.client.IncompleteRead(b"", 100)
        return data


def test_download_truncated_stream_is_a_plain_error(paths):
    with pytest.raises(AdbDownloadError, match="did not finish"):
        manager(paths).download(consent=True, opener=lambda url: TruncatedStream(GOOD_ZIP[:50]))


def test_download_masks_member_permissions(paths):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("platform-tools/adb", "platform-tools/loose"):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o4777) << 16  # setuid + world-writable
            zf.writestr(info, b"x")
    manager(paths).download(consent=True, opener=Opener(buf.getvalue()))
    for name in ("adb", "loose"):
        assert stat.S_IMODE((paths.platform_tools / name).stat().st_mode) & ~0o755 == 0


def test_download_sweeps_stale_leftovers(paths):
    (paths.support / ".platform-tools-abc.zip").write_bytes(b"x")
    (paths.support / ".platform-tools-abc.new").mkdir()
    (paths.support / ".platform-tools-abc.new" / "f").write_text("x")
    make_exe(paths.platform_tools / "adb")
    make_exe(paths.support / ".platform-tools.old" / "adb")
    manager(paths).download(consent=True, opener=Opener(GOOD_ZIP))
    assert [p.name for p in paths.support.iterdir() if p.name.startswith(".platform")] == []


def test_download_cleans_up_when_mkdtemp_fails(paths, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("sift.device.adb_manager.tempfile.mkdtemp", boom)
    with pytest.raises(AdbDownloadError):
        manager(paths).download(consent=True, opener=Opener(GOOD_ZIP))
    assert [p.name for p in paths.support.iterdir() if p.name.startswith(".")] == []


def test_concurrent_downloads_run_one_at_a_time(paths):
    active = 0
    peak = 0
    guard = threading.Lock()

    def opener(url: str) -> io.BytesIO:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.1)
        with guard:
            active -= 1
        return io.BytesIO(GOOD_ZIP)

    threads = [
        threading.Thread(target=lambda: manager(paths).download(consent=True, opener=opener))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert peak == 1 and (paths.platform_tools / "adb").exists()


def test_locate_restores_the_previous_install_after_an_interrupted_swap(paths):
    paths.platform_tools.rmdir()  # an install died between "set old aside" and "move new in"
    make_exe(paths.support / ".platform-tools.old" / "adb")
    loc = manager(paths).locate()
    assert loc.status == AdbStatus.ready and loc.path == paths.platform_tools / "adb"
    assert not (paths.support / ".platform-tools.old").exists()


def test_locate_restores_into_an_empty_platform_tools_folder(paths):
    make_exe(paths.support / ".platform-tools.old" / "adb")  # paths.ensure() left it empty
    assert manager(paths).locate().status == AdbStatus.ready


def test_locate_does_not_restore_a_broken_old_copy(paths):
    make_exe(paths.support / ".platform-tools.old" / "adb")
    loc = manager(paths, FakeRunner(version_rc=1)).locate()
    assert loc.status == AdbStatus.missing
    assert (paths.support / ".platform-tools.old" / "adb").exists()


# --- ensure_server --------------------------------------------------------------------------


def test_attach_never_runs_adb_cli_when_server_answers(paths):
    make_exe(paths.platform_tools / "adb")
    runner = FakeRunner()
    seen: list[tuple[str, int]] = []

    def probe(host: str, port: int) -> str | None:
        seen.append((host, port))
        return "41"

    assert manager(paths, runner, probe, adb_server_port=5037).ensure_server() == 5037
    assert runner.calls == []
    assert seen == [("127.0.0.1", 5037)]


def test_starts_server_once_when_none_answers(paths):
    adb = make_exe(paths.platform_tools / "adb")
    runner = FakeRunner()
    answers = iter([None, "41"])
    port = manager(paths, runner, lambda h, p: next(answers), adb_server_port=5099).ensure_server()
    assert port == 5099
    assert [c for c in runner.calls if "start-server" in c[0]] == [
        ([str(adb), "-P", "5099", "start-server"], 10)
    ]


def test_ensure_server_fails_plainly_when_nothing_ever_answers(paths):
    make_exe(paths.platform_tools / "adb")
    runner = FakeRunner(start_rc=1)
    with pytest.raises(AdbServerError, match="5037"):
        manager(paths, runner, lambda h, p: None).ensure_server()
    assert len([c for c in runner.calls if "start-server" in c[0]]) == 1


def test_ensure_server_without_adb_says_so(paths):
    with pytest.raises(AdbError, match="adb was not found"):
        manager(paths, probe=lambda h, p: None).ensure_server()


def test_ensure_server_start_timeout_is_a_plain_error(paths):
    make_exe(paths.platform_tools / "adb")

    def hang(args, timeout):
        if "version" in args:
            return subprocess.CompletedProcess(list(args), 0, VERSION_OUT, "")
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(AdbServerError):
        manager(paths, hang, lambda h, p: None).ensure_server()


def test_never_calls_kill_server(paths):
    make_exe(paths.platform_tools / "adb")
    runner = FakeRunner()  # one runner sees every call made by every scenario below
    for answers in ([None, "41"], [None, None], ["41"]):
        it = iter(answers)
        with contextlib.suppress(AdbError):
            manager(paths, runner, lambda h, p, it=it: next(it)).ensure_server()
    manager(paths, runner).locate()
    manager(paths, runner).download(consent=True, opener=Opener(GOOD_ZIP))
    assert runner.calls  # the scan below is not vacuous
    assert all("kill-server" not in arg for call, _ in runner.calls for arg in call)


# --- default probe (raw socket) ---------------------------------------------------------------


class OneShotServer:
    """Accepts one connection on 127.0.0.1:<free port>, reads the request, sends `reply`.

    With `hold`, the connection then stays open (silent) until the server is closed; with
    `trickle_s`, the reply goes out one byte at a time with that pause between bytes.
    """

    def __init__(self, reply: bytes, *, hold: bool = False, trickle_s: float = 0.0) -> None:
        self.reply = reply
        self.hold = hold
        self.trickle_s = trickle_s
        self.request = b""
        self._done = threading.Event()
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(5)
        self.port: int = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve)

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                self.request = conn.recv(64)
                if self.trickle_s:
                    for i in range(len(self.reply)):
                        if self._done.wait(self.trickle_s):
                            return
                        conn.sendall(self.reply[i : i + 1])
                elif self.reply:
                    conn.sendall(self.reply)
                if self.hold:
                    self._done.wait(10)
            except OSError:
                pass

    def __enter__(self) -> OneShotServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        self._srv.close()
        self._thread.join(10)
        assert not self._thread.is_alive()


FAST_PROBE = functools.partial(adb_server_version, deadline_s=0.3)


def test_probe_reads_the_server_version():
    with OneShotServer(b"OKAY00040029") as srv:
        assert adb_server_version("127.0.0.1", srv.port) == "41"
    assert srv.request == b"000chost:version"


@pytest.mark.parametrize(
    "reply",
    [
        b"garbage that is not adb",
        b"FAIL0004nope",
        b"OKAY0004zzzz",
        b"OKAYxxxx0029",
        b"OKAY00",  # closes mid-reply
        b"OKAY00040",  # closes mid-version
        b"",  # closes without a word
    ],
)
def test_probe_reports_a_busy_port_for_bad_replies(reply):
    with OneShotServer(reply) as srv, pytest.raises(AdbPortBusy):
        FAST_PROBE("127.0.0.1", srv.port)


def test_probe_reports_a_busy_port_when_the_listener_stays_silent():
    with OneShotServer(b"", hold=True) as srv, pytest.raises(AdbPortBusy):
        FAST_PROBE("127.0.0.1", srv.port)


def test_probe_stops_a_trickling_server_at_its_overall_deadline():
    started = time.monotonic()
    # Every byte arrives well within any per-read timeout, but the whole reply takes 2.4 s.
    with OneShotServer(b"OKAY00040029", hold=True, trickle_s=0.2) as srv:
        with pytest.raises(AdbPortBusy):
            adb_server_version("127.0.0.1", srv.port, deadline_s=0.5)
        elapsed = time.monotonic() - started
    assert elapsed < 1.5


def test_probe_has_an_overall_deadline_of_about_three_seconds():
    assert adb_server_version.__kwdefaults__["deadline_s"] == 3.0


def test_probe_returns_none_when_nothing_listens():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert adb_server_version("127.0.0.1", port) is None


@pytest.mark.parametrize(
    "server",
    [
        pytest.param({"reply": b"", "hold": True}, id="silent"),
        pytest.param({"reply": b"HTTP/1.1 400 Bad Request\r\n\r\n"}, id="garbled"),
    ],
)
def test_ensure_server_never_runs_adb_when_the_port_is_busy(paths, server):
    make_exe(paths.platform_tools / "adb")
    runner = FakeRunner()
    with OneShotServer(**server) as srv:
        mgr = manager(paths, runner, FAST_PROBE, adb_server_port=srv.port)
        with pytest.raises(AdbServerError) as info:
            mgr.ensure_server()
    assert info.value.message == (
        "Another program is using adb's port and is not answering. Close it or press Retry."
    )
    assert runner.calls == []


def test_ensure_server_starts_adb_once_when_nothing_listens(paths):
    adb = make_exe(paths.platform_tools / "adb")
    runner = FakeRunner()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(AdbServerError):  # the fake runner starts nothing, so nothing answers
        manager(paths, runner, FAST_PROBE, adb_server_port=port).ensure_server()
    assert [c for c in runner.calls if "start-server" in c[0]] == [
        ([str(adb), "-P", str(port), "start-server"], 10)
    ]


def test_default_manager_probe_is_the_raw_socket_one(paths):
    from sift.device import adb_manager

    assert AdbManager.__init__.__kwdefaults__["server_probe"] is adb_manager.adb_server_version


# --- API ------------------------------------------------------------------------------------


class FakeManager:
    def __init__(self) -> None:
        self.consents: list[bool] = []
        self.error: Exception | None = None

    def download(self, *, consent: bool, **_: object) -> AdbLocation:
        self.consents.append(consent)
        if self.error:
            raise self.error
        if not consent:
            raise ConsentRequired("Sift needs your OK first.")
        return AdbLocation(Path("/x/adb"), "35.0.2", AdbStatus.ready, "adb is ready.")


@pytest.fixture
def api(paths):
    client = client_for(make_ctx(paths))
    fake = FakeManager()
    client.app.dependency_overrides[get_adb_manager] = lambda: fake
    return client, fake


@pytest.mark.parametrize("body", [{}, {"consent": False}, None])
def test_api_download_without_consent_is_400(api, body):
    client, fake = api
    r = client.post("/api/devices/adb/download", headers=H, json=body)
    assert r.status_code == 400
    assert r.json()["code"] == "consent_required"
    assert r.json()["message"]
    assert fake.consents in ([], [False])  # never a download


def test_api_download_with_consent(api):
    client, fake = api
    r = client.post("/api/devices/adb/download", headers=H, json={"consent": True})
    assert r.status_code == 200
    assert r.json() == {
        "path": "/x/adb",
        "version": "35.0.2",
        "status": "ready",
        "message": "adb is ready.",
    }
    assert fake.consents == [True]


def test_api_download_failure_uses_the_error_shape(api):
    client, fake = api
    fake.error = AdbDownloadError("The download did not finish.")
    r = client.post("/api/devices/adb/download", headers=H, json={"consent": True})
    assert r.status_code == 502
    assert r.json() == {
        "code": "adb_download_failed",
        "message": "The download did not finish.",
        "detail": None,
    }


def test_api_download_requires_token(api):
    client, _ = api
    assert client.post("/api/devices/adb/download", json={"consent": True}).status_code == 401
