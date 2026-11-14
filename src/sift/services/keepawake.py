"""Keep the Mac, and optionally the phone, awake while a backup or task runs (spec 6).

The Mac: `caffeinate -i -w <sift pid>` holds an idle-sleep assertion while anything runs; it
also ends by itself if Sift exits. The phone: only when `phone.keep_awake` is on, Sift sets
`settings put global stay_on_while_plugged_in 3` and puts the previous value back when the last
job ends. The previous value is saved in the database first, so a crash is repaired at the next
launch (`restore_leftover`).
"""

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from sift.core.context import AppContext

log = logging.getLogger("sift.services")

STAY_ON = "stay_on_while_plugged_in"
STAY_ON_ALL = "3"  # AC and USB
SETTING_KEY = "phone_stay_on_previous"  # one row per phone: phone_stay_on_previous:<uuid>
_phone_lock = threading.Lock()  # saving, setting and restoring the phone's value never interleave

Spawn = Callable[[list[str]], Any]  # returns an object with terminate()


def _spawn(cmd: list[str]) -> Any:
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class KeepAwake:
    def __init__(self, ctx: AppContext, spawn: Spawn = _spawn) -> None:
        self._ctx = ctx
        self._spawn = spawn
        self._lock = threading.Lock()
        self._count = 0
        self._mac: Any = None

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        self._begin()
        try:
            yield
        finally:
            self._end()

    def _begin(self) -> None:
        with self._lock:
            self._count += 1
            if self._count > 1:
                return
            try:
                self._mac = self._spawn(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            except OSError as e:
                log.info("Could not keep the Mac awake: %s", e)
                self._mac = None
        if self._ctx.config.phone.keep_awake:
            self._phone_on()

    def _end(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)
            if self._count:
                return
            mac, self._mac = self._mac, None
        if mac is not None:
            with contextlib.suppress(Exception):
                mac.terminate()
                mac.wait(timeout=5)
        with self._lock:
            if self._count:  # a new job started meanwhile: it keeps the phone awake
                return
        restore_leftover(self._ctx)

    def _phone_on(self) -> None:
        session = _session(self._ctx)
        if session is None:
            return
        key = f"{SETTING_KEY}:{session.device_uuid}"
        with _phone_lock:
            try:
                with self._ctx.db.read() as conn:
                    saved = conn.execute("SELECT 1 FROM setting WHERE key = ?", (key,)).fetchone()
                if saved is None:  # an earlier value still saved is the phone's real one
                    previous = session.transport.global_setting(STAY_ON, None)
                    if previous is None:
                        return
                    with self._ctx.db.tx() as conn:  # saved first, so a crash can be repaired
                        conn.execute(
                            "INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
                            (key, previous),
                        )
                session.transport.global_setting(STAY_ON, STAY_ON_ALL)
            except Exception as e:
                log.info("Could not keep the phone awake: %s", e)


def _session(ctx: AppContext) -> Any:
    return ctx.tracker.session() if ctx.tracker is not None else None


def restore_leftover(ctx: AppContext) -> bool:
    """Put the connected phone's stay-awake setting back if Sift changed it (after a job, or
    after a crash). True when no phone has a value left to restore."""
    session = _session(ctx)
    with _phone_lock:
        with ctx.db.read() as conn:
            rows = conn.execute(
                "SELECT key, value FROM setting WHERE key LIKE ?", (f"{SETTING_KEY}:%",)
            ).fetchall()
        if not rows:
            return True
        mine = f"{SETTING_KEY}:{session.device_uuid}" if session is not None else None
        for key, previous in rows:
            if key != mine:
                continue  # another phone's value waits until that phone is connected
            try:
                session.transport.global_setting(STAY_ON, str(previous))
            except Exception as e:
                log.info("Could not restore the phone's stay-awake setting: %s", e)
                return False
            with ctx.db.tx() as conn:
                conn.execute("DELETE FROM setting WHERE key = ?", (key,))
            return len(rows) == 1
        return False


def restore_at_launch(ctx: AppContext, *, wait_s: float = 600.0, step_s: float = 5.0) -> None:
    """After a crash: wait (bounded) for the phone and put its setting back."""
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if restore_leftover(ctx):
            return
        time.sleep(step_s)
