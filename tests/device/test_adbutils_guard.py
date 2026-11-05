import gc
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
    first_create = _adb.AdbConnection._create_socket
    install_no_spawn_guard()
    assert _adb.AdbConnection._safe_connect is first
    assert _adb.AdbConnection._create_socket is first_create


def test_refused_connect_closes_its_socket():
    """Without the guard adbutils leaks the socket of a refused connect (a ResourceWarning).

    Warnings are errors in this suite, and the socket is collected before the test ends.
    """
    install_no_spawn_guard()
    client = adbutils.AdbClient(host="127.0.0.1", port=_free_port(), socket_timeout=2)
    for _ in range(3):
        with pytest.raises(AdbConnectionError):
            client.make_connection()
    gc.collect()


def test_refused_connect_socket_is_closed(monkeypatch):
    install_no_spawn_guard()
    port = _free_port()
    created: list[socket.socket] = []
    real_socket = socket.socket

    def tracking_socket(*args, **kwargs):
        s = real_socket(*args, **kwargs)
        created.append(s)
        return s

    monkeypatch.setattr(_adb.socket, "socket", tracking_socket)
    client = adbutils.AdbClient(host="127.0.0.1", port=port, socket_timeout=2)
    with pytest.raises(AdbConnectionError):
        client.make_connection()
    assert len(created) == 1
    assert created[0].fileno() == -1  # closed


def test_successful_connect_keeps_its_socket_open():
    install_no_spawn_guard()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        client = adbutils.AdbClient(host="127.0.0.1", port=listener.getsockname()[1])
        conn = client.make_connection(timeout=2)
        try:
            assert conn.conn.fileno() != -1
            assert conn.conn.gettimeout() == 2
        finally:
            conn.close()


def test_version_mismatch_fails_loudly(monkeypatch):
    monkeypatch.setattr(adbutils, "__version__", "9.9.9")
    with pytest.raises(RuntimeError, match=r"2\.12\.0"):
        install_no_spawn_guard()


@pytest.mark.parametrize("hook", ["_safe_connect", "_create_socket"])
def test_missing_hook_fails_loudly(monkeypatch, hook):
    monkeypatch.delattr(_adb.AdbConnection, hook)
    with pytest.raises(RuntimeError, match=hook):
        install_no_spawn_guard()


def test_manager_installs_the_guard(paths):
    from sift.core.config import PhoneConfig
    from sift.device.adb_manager import AdbManager

    AdbManager(paths, PhoneConfig())
    assert _adb.AdbConnection._safe_connect is adbutils_guard._connect_only
