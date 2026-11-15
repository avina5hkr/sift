"""SSD presence from synthetic diskutil output and a temp "/Volumes". No real drive is touched."""

import asyncio
from pathlib import Path

import pytest

from sift.core.context import AppContext
from sift.services.ssd import SsdWatcher, ssd_root
from tests.services.test_volumes import Diskutil

UUID = "11111111-2222-3333-4444-555555555555"


def watcher(ctx: AppContext, vols: Path, mounted: set[str], runner: Diskutil) -> SsdWatcher:
    return SsdWatcher(
        ctx, runner=runner, ismount=lambda p: p in mounted, volumes_dir=vols, interval_s=0.01
    )


def dest_rows(ctx: AppContext) -> list[tuple[str, str, str]]:
    with ctx.db.read() as conn:
        return [
            tuple(r) for r in conn.execute("SELECT kind, root_path, volume_uuid FROM destination")
        ]


def test_not_configured_never_runs_diskutil(ctx: AppContext, tmp_path: Path) -> None:
    runner = Diskutil()
    state = watcher(ctx, tmp_path, set(), runner).poll_once()
    assert (state.configured, state.connected) == (False, False)
    assert runner.calls == []


def test_found_by_uuid_at_a_real_mount_creates_its_destination(
    ctx: AppContext, tmp_path: Path
) -> None:
    ctx.config.backup.ssd_volume_uuid = UUID
    ctx.config.backup.mac_root = "~/PhoneBackup"
    (tmp_path / "Backup SSD").mkdir()
    state = watcher(
        ctx, tmp_path, {str(tmp_path / "Backup SSD"), "/Volumes/Backup SSD"}, Diskutil()
    ).poll_once()
    assert state.connected and state.name == "Backup SSD"
    assert state.root == str(ssd_root("/Volumes/Backup SSD", "~/PhoneBackup"))
    assert dest_rows(ctx) == [("ssd", "/Volumes/Backup SSD/PhoneBackup", UUID)]


def test_a_listed_volume_that_is_not_a_mount_point_is_not_used(
    ctx: AppContext, tmp_path: Path
) -> None:
    ctx.config.backup.ssd_volume_uuid = UUID
    (tmp_path / "Backup SSD").mkdir()
    state = watcher(ctx, tmp_path, {str(tmp_path / "Backup SSD")}, Diskutil()).poll_once()
    assert state.configured and not state.connected
    assert dest_rows(ctx) == []


def test_diskutil_runs_only_when_the_mounts_change(ctx: AppContext, tmp_path: Path) -> None:
    ctx.config.backup.ssd_volume_uuid = UUID
    runner = Diskutil()
    mounted: set[str] = set()
    w = watcher(ctx, tmp_path, mounted, runner)
    w.poll_once()
    first = len(runner.calls)
    w.poll_once()
    assert len(runner.calls) == first
    (tmp_path / "Backup SSD").mkdir()
    mounted.update({str(tmp_path / "Backup SSD"), "/Volumes/Backup SSD"})
    assert w.poll_once().connected
    assert len(runner.calls) > first


@pytest.mark.anyio
async def test_plug_and_unplug_publish_ssd_events_and_call_on_connect(
    ctx: AppContext, tmp_path: Path
) -> None:
    ctx.config.backup.ssd_volume_uuid = UUID
    mounted: set[str] = set()
    connected: list[str | None] = []
    w = SsdWatcher(
        ctx,
        runner=Diskutil(),
        ismount=lambda p: p in mounted,
        volumes_dir=tmp_path,
        on_connect=lambda s: connected.append(s.name),
    )
    w.poll_once()
    async with ctx.bus.subscribe() as sub:
        (tmp_path / "Backup SSD").mkdir()
        mounted.update({str(tmp_path / "Backup SSD"), "/Volumes/Backup SSD"})
        w.poll_once()
        ev = await asyncio.wait_for(sub.get(), 1)
        assert ev.name == "ssd" and '"connected":true' in ev.text
        (tmp_path / "Backup SSD").rmdir()
        mounted.clear()
        w.poll_once()
        ev = await asyncio.wait_for(sub.get(), 1)
        assert '"connected":false' in ev.text
    assert connected == ["Backup SSD"]


def test_status_reports_the_ssd(ctx: AppContext, tmp_path: Path) -> None:
    from tests.helpers import client_for

    ctx.config.backup.ssd_volume_uuid = UUID
    w = watcher(ctx, tmp_path, set(), Diskutil())
    w.poll_once()
    ctx.ssd = w
    body = client_for(ctx).get("/api/status", headers={"X-Sift-Token": "t0k"}).json()
    assert body["ssd"]["configured"] is True and body["ssd"]["connected"] is False


def test_eject_runs_diskutil_only_when_nothing_writes_to_the_ssd(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sift.backup.lease import DRIVE_LEASE
    from sift.server.routes import status as status_route
    from tests.helpers import client_for

    ctx.config.backup.ssd_volume_uuid = UUID
    (tmp_path / "Backup SSD").mkdir()
    mounted = {str(tmp_path / "Backup SSD"), "/Volumes/Backup SSD"}
    w = watcher(ctx, tmp_path, mounted, Diskutil())
    w.poll_once()
    ctx.ssd = w
    ejected: list[str] = []
    monkeypatch.setattr(status_route, "eject", lambda mount: ejected.append(mount))
    client = client_for(ctx)
    h = {"X-Sift-Token": "t0k"}
    assert client.get("/api/status", headers=h).json()["ssd"]["safe_to_unplug"] is True
    with DRIVE_LEASE.hold("test"):
        assert client.get("/api/status", headers=h).json()["ssd"]["safe_to_unplug"] is False
        assert client.post("/api/ssd/eject", headers=h).status_code == 409
    assert ejected == []
    assert client.post("/api/ssd/eject", headers=h).status_code == 200
    assert ejected == ["/Volumes/Backup SSD"]


def test_eject_is_refused_while_a_stuck_copy_holds_the_lease_after_its_job_ended(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sift.backup.lease import DRIVE_LEASE
    from sift.server.routes import status as status_route
    from tests.helpers import client_for

    ctx.config.backup.ssd_volume_uuid = UUID
    (tmp_path / "Backup SSD").mkdir()
    mounted = {str(tmp_path / "Backup SSD"), "/Volumes/Backup SSD"}
    w = watcher(ctx, tmp_path, mounted, Diskutil())
    w.poll_once()
    ctx.ssd = w
    ejected: list[str] = []
    monkeypatch.setattr(status_route, "eject", lambda mount: ejected.append(mount))
    client = client_for(ctx)
    h = {"X-Sift-Token": "t0k"}
    assert DRIVE_LEASE.acquire("backup", timeout_s=0)
    DRIVE_LEASE.hand_to_stuck("SSD copy (not answering)")  # no job is running any more
    try:
        assert client.get("/api/status", headers=h).json()["ssd"]["safe_to_unplug"] is False
        r = client.post("/api/ssd/eject", headers=h)
        assert r.status_code == 409
        assert "unplug" not in r.json()["message"].lower()
        assert "Quit and reopen Sift" in r.json()["message"]
    finally:
        DRIVE_LEASE.release()
    assert ejected == []


def test_after_a_run_the_database_is_copied_to_the_ssd(ctx: AppContext, tmp_path: Path) -> None:
    from sift.services import afterrun

    ctx.config.backup.ssd_volume_uuid = UUID
    root = tmp_path / "ssd-root"
    root.mkdir()

    class On:
        def state(self):
            from sift.services.ssd import SsdState

            return SsdState(True, True, "SSD", str(tmp_path), str(root), None, 1)

    ctx.ssd = On()  # type: ignore[assignment]

    class Snap:
        dry_run = False
        id = 1

    afterrun.after_run(ctx, Snap(), notifier=None, closing=lambda: True)  # type: ignore[arg-type]
    assert list((root / ".sift").glob("state-run-*.db"))
