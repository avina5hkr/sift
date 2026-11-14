"""M9 fault sweep (spec 16): a backup under every FakeTransport fault keeps the invariants.

After each faulted run: nothing was written to or deleted on the phone; every replica row points
to a file whose bytes hash to its content (I1, I2: no half or wrong copies); the run ended
(no hang). Then a clean run backs up everything that is left.
"""

import hashlib
from pathlib import Path

import pytest

from sift.backup.run import usable_dests
from sift.backup.summary import reset_failed
from tests.backup.test_run import Rig, finished

SETS = ["Camera", "WhatsApp media", "Pictures and Movies"]


def some_paths(rig: Rig, n: int = 3) -> set[str]:
    files = sorted(p for p, f in rig.transport.phone.files.items()
                   if f.is_symlink_to is None and "/DCIM/" in p)  # fmt: skip
    return set(files[:n])


def apply(rig: Rig, fault: str) -> None:
    f = rig.transport.faults
    picked = some_paths(rig)
    if fault == "disconnect_after_bytes":
        f.disconnect_after_bytes = 2000
    elif fault == "offline_for_s":
        f.offline_for_s = 0.2
    elif fault == "identity":
        f.identity = "SOMEONE-ELSE"
    elif fault == "garbage_output":
        f.garbage_output = True
    elif fault == "short_reads":
        f.short_reads = True
    elif fault == "slow_read_bps":
        f.slow_read_bps = 5_000_000
    elif fault == "hang_on":  # a read that never yields is given up after the stall limit
        rig.transport.stall_timeout_s = 0.5
        f.hang_on.update(picked)
    else:  # path sets: size_change_mid_read, wrong_sha256, unstatable, lists_empty
        getattr(f, fault).update(picked if fault != "lists_empty" else {"/sdcard/DCIM/Camera"})


def replica_problems(rig: Rig) -> list[str]:
    roots = {d.id: Path(d.root) for d in usable_dests(rig.ctx)}
    bad = []
    with rig.ctx.db.read() as conn:
        for digest, dest_id, rel, size in conn.execute(
            "SELECT content_hash, dest_id, rel_path, size FROM replica"
        ):
            path = roots[int(dest_id)] / rel
            try:
                data = path.read_bytes()
            except OSError:
                bad.append(f"missing {rel}")
                continue
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                bad.append(f"wrong bytes {rel}")
    return bad


def unbacked(rig: Rig) -> list[str]:
    with rig.ctx.db.read() as conn:
        return [str(r[0]) for r in conn.execute(
            "SELECT remote_path FROM phone_file WHERE present = 1 AND state != 'done' "
            "AND state != 'skipped'"
        )]  # fmt: skip


FAULTS = [
    "disconnect_after_bytes", "offline_for_s", "identity", "garbage_output", "short_reads",
    "slow_read_bps", "size_change_mid_read", "wrong_sha256", "unstatable", "hang_on",
    "lists_empty",
]  # fmt: skip


@pytest.mark.parametrize("fault", FAULTS)
def test_a_backup_under_each_fault_keeps_the_invariants(rig: Rig, fault: str) -> None:
    apply(rig, fault)
    jobs = rig.jobs()
    snap = finished(jobs, jobs.start_backup(SETS))  # ends: done, or failed with a reason
    assert str(snap.state) in ("done", "failed", "stopped"), snap
    assert not [e for e in rig.transport.journal if e.mutating], "a backup wrote to the phone"
    assert replica_problems(rig) == []
    # the fault clears (cable back in, file settled): a clean run finishes the job
    rig.transport.faults.__init__()  # type: ignore[misc]
    if fault == "size_change_mid_read":  # those files really changed: let them settle
        rig.ctx.config.backup.settle_seconds = 0
    reset_failed(rig.ctx.db, 1, SETS)  # the owner's "Retry failed" for files out of retries
    finished(jobs, jobs.start_backup(SETS))
    if unbacked(rig):  # a file that changed under the fault needs one more settled pass
        finished(jobs, jobs.start_backup(SETS))
    assert replica_problems(rig) == []
    assert unbacked(rig) == [], fault
