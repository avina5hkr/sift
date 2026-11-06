"""The first-run reader test: how many parallel reads get the most out of this phone.

Rounds of 1, 2 and 4 readers, 3 s each, read the largest backed-up-set files on the phone, each
reader through its own stream. **The bytes are read from the phone and thrown away**: nothing
is opened, created or staged on the Mac, and no function here takes a `Path`. The fastest
round's reader count is stored as `phone.parallel_reads_usb`, and its speed as
`phone.measured_read_bps` (the time estimate's basis until a job measures its own speed).
"""

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sift.core.config import save_config
from sift.core.context import AppContext
from sift.core.db import Database
from sift.device.session import DeviceSession
from sift.device.transport import FileChanged, RemoteEntry, RemoteError, Transport

COUNTS = (1, 2, 4)
ROUND_S = 3.0
LARGEST_FILES = 8
_CHUNK = 1 << 20


@dataclass(frozen=True)
class ReaderTestResult:
    readers: int  # the fastest reader count
    speed_bps: float  # what it reached, in bytes per second
    speeds: tuple[tuple[int, float], ...]  # (readers, bytes per second) for every round


def largest_files(db: Database, device_uuid: str, limit: int = LARGEST_FILES) -> list[RemoteEntry]:
    """The phone's largest files the last scan saw (present), biggest first."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT p.remote_path, p.size, p.mtime FROM phone_file p JOIN device d "
            "ON d.id = p.device_id WHERE d.uuid = ? AND p.present = 1 AND p.size > 0 "
            "ORDER BY p.size DESC, p.remote_path LIMIT ?",
            (device_uuid, limit),
        ).fetchall()
    return [RemoteEntry(r[0], int(r[1]), int(r[2]), is_dir=False, is_symlink=False) for r in rows]


def _round(
    transport: Transport,
    files: Sequence[RemoteEntry],
    readers: int,
    duration_s: float,
    monotonic: Callable[[], float],
) -> float:
    """Bytes per second that `readers` parallel streams read in `duration_s`."""
    totals = [0] * readers
    errors: list[BaseException] = []
    start = monotonic()
    deadline = start + duration_s

    def read(index: int) -> None:
        turn, misses = index, 0
        try:
            while monotonic() < deadline and misses < len(files):
                entry = files[turn % len(files)]
                turn += readers
                try:
                    stream = transport.open_read(entry.path, entry=entry)
                except RemoteError:
                    misses += 1
                    continue
                try:
                    while monotonic() < deadline:
                        data = stream.read(_CHUNK)
                        if not data:
                            break
                        totals[index] += len(data)  # counted, then dropped
                    misses = 0
                except FileChanged:
                    misses += 1
                finally:
                    stream.close()
        except BaseException as exc:  # a disconnect: the test as a whole fails
            errors.append(exc)

    threads = [
        threading.Thread(target=read, args=(i,), name=f"sift-reader-test-{i}", daemon=True)
        for i in range(readers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return sum(totals) / max(monotonic() - start, 1e-9)


def measure_readers(
    transport: Transport,
    files: Sequence[RemoteEntry],
    *,
    counts: Sequence[int] = COUNTS,
    duration_s: float = ROUND_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReaderTestResult:
    """Read `files` with each reader count in turn; the fastest wins (fewer readers on a tie)."""
    if not files:
        raise ValueError("the reader test needs at least one file")
    speeds = tuple((n, _round(transport, files, n, duration_s, monotonic)) for n in counts)
    readers, speed = max(speeds, key=lambda s: (s[1], -s[0]))
    return ReaderTestResult(readers, speed, speeds)


def store_result(ctx: AppContext, result: ReaderTestResult) -> None:
    """Save the reader count and the speed in the settings."""
    phone = ctx.config.phone
    phone.parallel_reads_usb = result.readers
    phone.measured_read_bps = int(result.speed_bps)
    save_config(ctx.config, ctx.paths.config)


def stored_read_speed(ctx: AppContext) -> float | None:
    """The reader test's speed in bytes per second, or None before it ran."""
    bps = ctx.config.phone.measured_read_bps
    return float(bps) if bps > 0 else None


def run_reader_test(
    ctx: AppContext,
    session: DeviceSession,
    *,
    duration_s: float = ROUND_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReaderTestResult | None:
    """Measure on the phone's largest files and store the result; None when none are known yet.

    The rounds never use more readers than the phone's connection slots allow, keeping one for
    everything else.
    """
    files = largest_files(ctx.db, session.device_uuid)
    if not files:
        return None
    limits = getattr(session.transport, "limits", None)
    most = getattr(limits, "max_connections", None)
    counts = [n for n in COUNTS if most is None or n <= max(1, most - 1)]
    result = measure_readers(
        session.transport, files, counts=counts, duration_s=duration_s, monotonic=monotonic
    )
    store_result(ctx, result)
    return result
