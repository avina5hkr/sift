"""Backup confirmation on the fake phone: checks 1 to 3, the Markdown report, going stale."""

from sift.backup.confirm import build_report, confirm, latest, required_kinds
from sift.backup.replicator import Dest
from sift.backup.run import mac_target
from sift.core.clock import ManualClock
from tests.backup.test_run import Rig, finished

DAY = 86_400


def device_id(rig: Rig) -> int:
    with rig.ctx.db.read() as conn:
        return int(conn.execute("SELECT id FROM device WHERE uuid = 'dev-1'").fetchone()[0])


def backed_up(rig: Rig, sets: list[str]) -> None:
    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(sets))


def test_required_destinations_follow_whether_an_ssd_was_chosen(rig: Rig) -> None:
    assert required_kinds(rig.ctx.config) == ["mac"]
    rig.ctx.config.backup.ssd_volume_uuid = "uuid"
    assert required_kinds(rig.ctx.config) == ["mac", "ssd"]


def test_a_fully_backed_up_set_is_confirmed_with_a_report(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    clock = ManualClock(1_800_000_000)
    target = mac_target(rig.ctx.db, rig.ctx.config)
    mac = Dest(target.dest_id, "mac", target.root)
    done = confirm(
        rig.ctx.db,
        rig.ctx.config,
        device_id(rig),
        "Camera",
        rig.transport,
        dests=[mac],
        clock=clock,
    )
    assert done.confirmed and done.files > 0
    reports = list((mac_target(rig.ctx.db, rig.ctx.config).root / ".sift/reports").glob("*.md"))
    assert len(reports) == 1 and "**Confirmed.**" in reports[0].read_text()
    assert (
        latest(rig.ctx.db, rig.ctx.config, device_id(rig), "Camera", clock=clock).stale_reason
        is None
    )
    # a re-confirmation asks the phone about nothing that was already checked
    rig.transport.journal.clear()
    confirm(rig.ctx.db, rig.ctx.config, device_id(rig), "Camera", rig.transport, clock=clock)
    assert [e for e in rig.transport.journal if e.op == "sha256"] == []
    assert not any(e.mutating for e in rig.transport.journal)


def test_without_the_ssd_copy_a_set_is_not_confirmed(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    rig.ctx.config.backup.ssd_volume_uuid = "uuid"  # an SSD is required now, and holds nothing
    report = build_report(
        rig.ctx.db, rig.ctx.config, device_id(rig), "Camera", transport=rig.transport
    )
    assert not report.confirmed and report.missing["ssd"] == report.on_phone


def test_a_file_changed_on_the_phone_blocks_confirmation(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    path = next(p for p in rig.transport.phone.files if p.startswith("/sdcard/DCIM/Camera/"))
    file = rig.transport.phone.files[path]
    rig.transport.phone.modify(path, b"x" * file.size, file.mtime + 1)
    report = build_report(
        rig.ctx.db, rig.ctx.config, device_id(rig), "Camera", transport=rig.transport
    )
    assert not report.confirmed and report.phone_hash_mismatch == 1


def test_confirmation_goes_stale(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    clock = ManualClock(1_800_000_000)
    dev = device_id(rig)
    confirm(rig.ctx.db, rig.ctx.config, dev, "Camera", rig.transport, clock=clock)
    clock.advance(31 * DAY)
    assert "30 days" in latest(rig.ctx.db, rig.ctx.config, dev, "Camera", clock=clock).stale_reason
    confirm(rig.ctx.db, rig.ctx.config, dev, "Camera", rig.transport, clock=clock)
    with rig.ctx.db.tx() as conn:  # as a real scan leaves it: present and not backed up yet
        conn.execute(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, present,"
            " set_name) VALUES (?, '/sdcard/DCIM/Camera/new2.jpg', 'camera', 3, 1, 'discovered', 1,"
            " 'Camera')",
            (dev,),
        )
    assert "changed" in latest(rig.ctx.db, rig.ctx.config, dev, "Camera", clock=clock).stale_reason


def test_copies_on_a_retired_ssd_do_not_count(rig: Rig, tmp_path) -> None:
    from tests.backup.test_run import FakeSsd

    old = FakeSsd(rig, tmp_path / "old-ssd" / "backup")
    rig.ctx.ssd = old  # type: ignore[assignment]
    backed_up(rig, ["Camera"])  # copies land on the old SSD too
    report = build_report(
        rig.ctx.db, rig.ctx.config, device_id(rig), "Camera", transport=rig.transport
    )
    assert report.confirmed
    rig.ctx.config.backup.ssd_volume_uuid = "a-new-ssd"  # the owner replaced the SSD
    report = build_report(
        rig.ctx.db, rig.ctx.config, device_id(rig), "Camera", transport=rig.transport
    )
    assert not report.confirmed and report.missing["ssd"] == report.on_phone
