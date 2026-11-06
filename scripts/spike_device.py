"""Read-only real-phone checks for the M1 device layer.

Usage: SIFT_REALPHONE_SERIAL=<serial> uv run python scripts/spike_device.py

Every step only reads from the phone. The output holds counts, timings and
yes/no answers only: no file names, no serial numbers, no model names, so it
can be summarised in a public document.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from sift.device.adb_transport import AdbTransport, Limits
from sift.device.transport import DeviceTimeout, RemoteEntry, RemoteError, TransportError
from sift.services.usblink import link_speed_mbps

PORT = 5037
MB = 1_000_000
SET_ROOTS = [
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Movies",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Media",
    "/sdcard/WhatsApp/Media",
    "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media",
    "/sdcard/Documents",
    "/sdcard/Download",
    "/sdcard/Recordings",
]


def out(label: str, value: Any) -> None:
    print(f"{label}: {value}", flush=True)


def time_wait_count() -> int:
    r = subprocess.run(["netstat", "-an", "-p", "tcp"], capture_output=True, text=True, check=False)
    return sum(1 for line in r.stdout.splitlines() if ".5037" in line and "TIME_WAIT" in line)


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    t = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t


def stream_hash(
    t: AdbTransport, path: str, offset: int = 0, keep: tuple[int, int] | None = None
) -> tuple[str, int, float, bytes]:
    """Hash a file from `offset`; returns (sha, bytes, seconds to first byte, kept slice)."""
    h = hashlib.sha256()
    total = 0
    kept = b""
    start = time.perf_counter()
    first = -1.0
    s = t.open_read(path, offset)
    try:
        while True:
            chunk = s.read(1 << 20)
            if not chunk:
                break
            if first < 0:
                first = time.perf_counter() - start
            if keep is not None:
                lo, hi = keep
                a, b = max(lo, offset + total), min(hi, offset + total + len(chunk))
                if a < b:
                    kept += chunk[a - offset - total : b - offset - total]
            h.update(chunk)
            total += len(chunk)
    finally:
        s.close()
    return h.hexdigest(), total, first, kept


def main() -> int:
    serial = os.environ.get("SIFT_REALPHONE_SERIAL", "")
    if not serial:
        print("Set SIFT_REALPHONE_SERIAL.")
        return 2
    t = AdbTransport(serial, PORT, limits=Limits())

    # 11 / 19: info() shapes and identity
    info = t.info()
    out("info.serial_present", bool(info.serial))
    out("identity_equals_adb_serial", info.serial == serial)
    out("info.model_present", bool(info.model))
    out("info.android_version", info.android_version)
    out("info.battery_present", info.battery_pct is not None)
    out("info.df_present", info.free_bytes is not None and info.total_bytes is not None)
    out("info.screen_sizes_count", len(info.screen_sizes))
    speed = link_speed_mbps(serial)
    out("usb_serial_matches_adb_serial", speed is not None)
    out("link_speed_mbps", speed)
    out("link_health.kind", t.link_health().kind)  # 12

    # 5: /sdcard symlink and listing through it
    root = t.stat("/sdcard")
    out("sdcard_is_symlink", bool(root and root.is_symlink))
    top, secs = timed(lambda: t.list_dir("/sdcard"))
    out("sdcard_children", len(top))
    out("sdcard_children_paths_prefixed", all(e.path.startswith("/sdcard/") for e in top))

    # 18 / 6 / 14: walk every default set root; one connection per walk (TIME_WAIT delta)
    files: list[RemoteEntry] = []
    for r in SET_ROOTS:
        before = time_wait_count()
        try:
            entries, secs = timed(lambda r=r: list(t.walk(r)))
            out(
                f"walk[{SET_ROOTS.index(r)}] files/seconds/new_TIME_WAIT",
                (len(entries), round(secs, 1), time_wait_count() - before),
            )
            files += entries
        except RemoteError as e:
            out(f"walk[{SET_ROOTS.index(r)}] error", type(e).__name__)
    regular = [e for e in files if not e.is_symlink and not e.is_dir]
    out("walked_files_total", len(regular))
    out(
        "non_utf8_names_seen",
        sum(1 for e in regular if any(0xDC80 <= ord(c) <= 0xDCFF for c in e.path)),
    )

    # 7: another app's private folder lists as empty or errors
    try:
        data = t.list_dir("/sdcard/Android/data")
        out("android_data_children", len(data))
        if data:
            try:
                out("android_data_child_entries", len(t.list_dir(data[0].path)))
            except RemoteError as e:
                out("android_data_child", type(e).__name__)
    except RemoteError as e:
        out("android_data", type(e).__name__)

    # 1 / 2 / 16: hash parity, missing path → None, cold sha rate over >= 50 files
    out(
        "sha_missing_is_none",
        t.sha256(["/sdcard/.sift-check-missing-file"]).get("/sdcard/.sift-check-missing-file", "x")
        is None,
    )
    sample = sorted((e for e in regular if 1 * MB < e.size < 200 * MB), key=lambda e: e.path)[::7][
        :60
    ]
    phone, secs = timed(lambda: t.sha256([e.path for e in sample]))
    nbytes = sum(e.size for e in sample)
    out(
        "sha_batch_files/bytes/seconds/MBps",
        (len(sample), nbytes, round(secs, 2), round(nbytes / MB / secs) if secs else None),
    )
    parity = 0
    stream_secs = 0.0
    stream_bytes = 0
    for e in sample[:10]:
        (sha, n, _first, _), secs = timed(lambda e=e: stream_hash(t, e.path))
        stream_secs += secs
        stream_bytes += n
        parity += sha == phone.get(e.path)
    out("hash_parity_of_10", parity)
    out("stream_MBps", round(stream_bytes / MB / stream_secs, 1) if stream_secs else None)

    # 17: small-file throughput (>= 1000 small files if available)
    small = [e for e in regular if e.size < 200_000][:1000]
    before = time_wait_count()
    t0 = time.perf_counter()
    for e in small:
        s = t.open_read(e.path)
        try:
            while s.read(1 << 20):
                pass
        finally:
            s.close()
    secs = time.perf_counter() - t0
    out(
        "small_files/seconds/files_per_s/new_TIME_WAIT",
        (
            len(small),
            round(secs, 1),
            round(len(small) / secs, 1) if secs else None,
            time_wait_count() - before,
        ),
    )

    # 4 / 3: >4 GiB RECV with hash, then tail TTFB at offsets 1, ~1 GB, >4 GiB
    big = max(regular, key=lambda e: e.size)
    out("largest_file_GiB", round(big.size / 2**30, 2))
    if big.size > 2**31:
        slice_at = min(3_500_000_000, big.size - 2 * MB)
        (sha_full, n, _, kept), secs = timed(
            lambda: stream_hash(t, big.path, keep=(slice_at, slice_at + MB))
        )
        out("big_recv_complete", n == big.size)
        out("big_recv_seconds/MBps", (round(secs, 1), round(n / MB / secs, 1)))
        phone_big, hsecs = timed(lambda: t.sha256([big.path]))
        out("big_hash_matches_phone", sha_full == phone_big.get(big.path))
        out("big_phone_hash_MBps", round(big.size / MB / hsecs) if hsecs else None)
        for off in (1, 10**9, slice_at):  # 3.5 GB or near the end
            s = t.open_read(big.path, off)
            try:
                t1 = time.perf_counter()
                head = s.read(MB)
                ttfb = time.perf_counter() - t1
            finally:
                s.close()
            ok = (head[: len(kept)] == kept[: len(head)]) if off == slice_at else None
            out(f"tail_offset_{off}_ttfb_s/bytes_match", (round(ttfb, 2), ok))

    # 9 (part): six streams open, a seventh call times out cleanly
    short = Limits(stat_s=3)
    t7 = AdbTransport(serial, PORT, limits=short)
    opened = [t7.open_read(e.path) for e in sample[:6]]
    try:
        t7.stat(sample[0].path)
        out("seventh_call", "succeeded (unexpected)")
    except DeviceTimeout:
        out("seventh_call", "DeviceTimeout")
    finally:
        for s in opened:
            s.close()
    out("after_close_stat_works", t7.stat(sample[0].path) is not None)

    # 10: a request close to 64 KiB is accepted (read-only no-op command)
    pad = "x" * 60_000
    service = f"exec:true #{pad}".encode()
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as c:
        req = f"host:transport:{serial}".encode()
        c.sendall(f"{len(req):04x}".encode() + req)
        ok1 = c.recv(4) == b"OKAY"
        c.sendall(f"{len(service):04x}".encode() + service)
        ok2 = c.recv(4) == b"OKAY"
    out("request_60KiB_accepted", ok1 and ok2)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TransportError as e:
        print(f"stopped: {type(e).__name__}")
        sys.exit(1)
