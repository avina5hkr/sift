import socket
import subprocess

import adbutils
import pytest
from adbutils import _adb
from adbutils.errors import AdbConnectionError, AdbTimeout

from sift.device import adbutils_guard
from sift.device.adbutils_guard import install_no_spawn_guard


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


# adbutils itself leaves the socket open when a connect is refused; that is its leak, not ours.
@pytest.mark.filterwarnings("ignore::ResourceWarning")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_guarded_adbutils_never_spawns_a_process(monkeypatch):
    calls: list[object] = []

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("a subprocess was started")

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(subprocess, "Popen", spy)
    install_no_spawn_guard()
    client = adbutils.AdbClient(host="127.0.0.1", port=_free_port(), socket_timeout=2)
    with pytest.raises((AdbConnectionError, AdbTimeout)):
        client.server_version()
    assert calls == []


def test_guard_is_idempotent():
    install_no_spawn_guard()
    first = _adb.AdbConnection._safe_connect
    install_no_spawn_guard()
    assert _adb.AdbConnection._safe_connect is first


def test_version_mismatch_fails_loudly(monkeypatch):
    monkeypatch.setattr(adbutils, "__version__", "9.9.9")
    with pytest.raises(RuntimeError, match=r"2\.12\.0"):
        install_no_spawn_guard()


def test_missing_hook_fails_loudly(monkeypatch):
    monkeypatch.delattr(_adb.AdbConnection, "_safe_connect")
    with pytest.raises(RuntimeError, match="_safe_connect"):
        install_no_spawn_guard()


def test_manager_installs_the_guard(paths):
    from sift.core.config import PhoneConfig
    from sift.device.adb_manager import AdbManager

    AdbManager(paths, PhoneConfig())
    assert _adb.AdbConnection._safe_connect is adbutils_guard._connect_only
