"""M9 benchmarks (spec 16 performance): `make bench`. Not part of the normal test run.

Numbers are printed; the assertions are only loose floors that catch a large regression.
"""

import time

import pytest

from sift.device.fake_phone import build_fake_phone
from sift.device.fake_transport import FakeTransport
from tests.backup.test_run import Rig


def finished(jobs, job_id):  # benchmarks may take minutes
    assert jobs.wait_finished(job_id, 600)
    return next(s for s in jobs.snapshot() if s.id == job_id)


pytestmark = pytest.mark.bench


def test_bench_backup_of_a_fake_phone(rig: Rig) -> None:
    rig.transport.phone = build_fake_phone(3000)
    rig.transport.__init__(rig.transport.phone)  # type: ignore[misc]
    jobs = rig.jobs()
    sets = [s.name for s in rig.ctx.config.backup.sets if s.enabled]
    start = time.perf_counter()
    snap = finished(jobs, jobs.start_backup(sets))
    took = time.perf_counter() - start
    with rig.ctx.db.read() as conn:
        n, size = conn.execute(
            "SELECT count(*), coalesce(sum(size), 0) FROM phone_file WHERE state = 'done'"
        ).fetchone()
    print(f"\nbackup: {n} files, {size / 1e6:.0f} MB in {took:.1f} s = {n / took:.0f} files/s")
    assert str(snap.state) == "done" and n > 1000 and took < 120
    start = time.perf_counter()
    finished(jobs, jobs.start_backup(sets))
    again = time.perf_counter() - start
    print(f"second backup, nothing changed: {again:.1f} s")
    assert again < took


def test_bench_scan_listing_speed() -> None:
    phone = build_fake_phone(20000)
    t = FakeTransport(phone)
    start = time.perf_counter()
    n = sum(1 for e in t.walk("/sdcard", on_error=lambda e: None) if not e.is_dir)
    took = time.perf_counter() - start
    print(f"\nlisting: {n} files in {took:.2f} s")
    assert n > 10000 and took < 30
