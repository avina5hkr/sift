"""The first-run wizard's API, over the fake phone and scripted device lists. Nothing here reaches
adb, a real volume, the network or a real home folder."""

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sift.backup.readers import run_reader_test
from sift.core.config import load_config
from sift.core.context import AppContext
from sift.core.jobs import JobState
from sift.core.paths import AppPaths
from sift.device.fake_phone import BIG_VIDEO, FakePhone, build_fake_phone
from sift.device.fake_transport import FakeTransport
from sift.device.session import DeviceSession, PhoneIdentity
from sift.device.track_devices import Snapshot
from sift.device.tracker import DeviceTracker
from sift.device.transport import Transport
from sift.server.routes import setup as setup_routes
from sift.server.routes.backup import _run_lock
from sift.server.routes.setup import get_volume_lister
from sift.services.volumes import Volume
from tests.helpers import H, client_for, make_ctx
from tests.server.test_backup_api import Tracker, real_jobs

A = "AAAA-0001"
B = "BBBB-0002"


class StaticEvents:
    """One fixed device list, then idle ticks until closed."""

    def __init__(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[Snapshot | None]:
        yield dict(self._snapshot)
        while not self._closed.wait(0.02):
            yield None

    def close(self) -> None:
        self._closed.set()


def wait_for(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.fixture
def ctx(paths: AppPaths) -> AppContext:
    return make_ctx(paths)


@pytest.fixture
def client(ctx: AppContext) -> TestClient:
    return client_for(ctx)


def start_tracker(ctx: AppContext, snapshot: Snapshot) -> DeviceTracker:
    phones: dict[str, Transport] = {
        A: FakeTransport(FakePhone(serial=A, model="Alpha 1")),
        B: FakeTransport(FakePhone(serial=B, model="Beta 2")),
    }
    tracker = DeviceTracker(
        ctx,
        events=lambda: StaticEvents(snapshot),
        connect=lambda serial: phones[serial],
        usb_present=lambda: False,
        link_speed=lambda _s: 5000,
        sleep=lambda _s: time.sleep(0.01),
    )
    ctx.tracker = tracker
    tracker.start()
    return tracker


@pytest.fixture
def two_phones(ctx: AppContext) -> Iterator[DeviceTracker]:
    tracker = start_tracker(ctx, {A: "device", B: "device", "emulator-5554": "device"})
    wait_for(lambda: len(tracker.attached_phones()) == 2)
    yield tracker
    tracker.stop()


def get(client: TestClient, path: str) -> Any:
    return client.get(f"/api/setup/{path}", headers=H)


def post(client: TestClient, path: str, body: Any = None) -> Any:
    return client.post(f"/api/setup/{path}", headers=H, json=body)


# -- every route needs the token ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "state"),
        ("POST", "complete"),
        ("GET", "adb"),
        ("GET", "devices"),
        ("POST", "phone"),
        ("POST", "switch-phone"),
        ("GET", "folder"),
        ("POST", "mac-folder"),
        ("GET", "volumes"),
        ("POST", "ssd"),
        ("GET", "sets"),
        ("POST", "scan"),
        ("POST", "sets"),
        ("POST", "reader-test"),
    ],
)
def test_every_setup_route_needs_the_token(client: TestClient, method: str, path: str) -> None:
    resp = client.request(method, f"/api/setup/{path}", json={} if method == "POST" else None)
    assert resp.status_code == 401
    assert resp.json()["code"] == "bad_token"


# -- where setup stands --------------------------------------------------------------------------


def test_a_fresh_install_is_not_set_up(client: TestClient) -> None:
    state = get(client, "state").json()
    assert state["complete"] is False
    assert state["phone"] is None
    assert state["mac_root"] == "~/PhoneBackup"
    assert state["suggested_label"] == "Phone"
    assert state["ssd_volume_uuid"] == ""


def test_completing_setup_is_remembered(client: TestClient, ctx: AppContext) -> None:
    assert post(client, "complete").json()["complete"] is True
    assert get(client, "state").json()["complete"] is True
    with ctx.db.read() as conn:
        assert conn.execute("SELECT value FROM setting WHERE key = 'setup_complete'").fetchone()


def test_the_state_names_the_stored_phone_by_its_last_four_only(
    client: TestClient, ctx: AppContext
) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO device (uuid, serial, model) VALUES ('u1', 'SERIAL-ABCD1234', 'Alpha 1')"
        )
    resp = get(client, "state")
    assert resp.json()["phone"] == {
        "uuid": "u1",
        "model": "Alpha 1",
        "last4": "1234",
        "label": None,
    }
    assert resp.json()["suggested_label"] == "Alpha 1"
    assert "SERIAL-ABCD1234" not in resp.text


# -- adb -----------------------------------------------------------------------------------------


def test_adb_state_shows_the_size_before_any_download(client: TestClient) -> None:
    from sift.device.adb_manager import AdbLocation, AdbStatus
    from sift.server.routes.devices import get_adb_manager

    class Missing:
        def locate(self) -> AdbLocation:
            return AdbLocation(None, None, AdbStatus.missing, "adb was not found.")

        def download(self, **_: object) -> None:
            raise AssertionError("nothing may be downloaded by looking")

    client.app.dependency_overrides[get_adb_manager] = lambda: Missing()  # type: ignore[attr-defined]
    body = get(client, "adb").json()
    assert body["status"] == "missing"
    assert body["version"] is None
    assert body["download_bytes"] > 1_000_000
    assert body["download_host"] == "dl.google.com"


# -- the device picker ---------------------------------------------------------------------------


def test_device_picker_lists_only_non_emulator_devices(
    client: TestClient, two_phones: DeviceTracker
) -> None:
    resp = get(client, "devices")
    phones = resp.json()["phones"]
    assert [(p["model"], p["last4"], p["ready"]) for p in phones] == [
        ("Alpha 1", "0001", True),
        ("Beta 2", "0002", True),
    ]
    assert not any("emulator" in json.dumps(p) for p in phones)
    assert A not in resp.text and B not in resp.text  # the serial never leaves, only 4 characters


def test_the_picker_says_when_a_phone_waits_for_allow(ctx: AppContext, client: TestClient) -> None:
    tracker = start_tracker(ctx, {A: "device", B: "unauthorized"})
    try:
        wait_for(lambda: len(tracker.attached_phones()) == 2)
        phones = get(client, "devices").json()["phones"]
        assert [(p["ready"], p["waiting_for_allow"]) for p in phones] == [
            (True, False),
            (False, True),
        ]
    finally:
        tracker.stop()


def test_the_picker_needs_a_tracker(client: TestClient) -> None:
    resp = get(client, "devices")
    assert resp.status_code == 409 and resp.json()["code"] == "no_tracker"


def stored_uuid(ctx: AppContext) -> list[tuple[str, str]]:
    with ctx.db.read() as conn:
        return [(r[0], r[1]) for r in conn.execute("SELECT serial, model FROM device ORDER BY id")]


def ref_of(client: TestClient, model: str) -> str:
    return next(p["ref"] for p in get(client, "devices").json()["phones"] if p["model"] == model)


def test_device_picker_choice_becomes_stored_phone(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    assert stored_uuid(ctx) == []  # two phones: the tracker stores neither
    resp = post(client, "phone", {"ref": ref_of(client, "Beta 2")})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["model"], resp.json()["last4"]) == ("Beta 2", "0002")
    assert stored_uuid(ctx) == [(B, "Beta 2")]
    assert get(client, "state").json()["phone"]["model"] == "Beta 2"
    wait_for(lambda: two_phones.status().serial == B)
    assert two_phones.status().state.value == "connected_usb"
    stored = [p["stored"] for p in get(client, "devices").json()["phones"]]
    assert stored == [False, True]


def test_choosing_a_phone_that_is_gone_is_a_404_in_plain_words(
    client: TestClient, two_phones: DeviceTracker
) -> None:
    resp = post(client, "phone", {"ref": "0" * 16})
    assert resp.status_code == 404
    assert resp.json()["code"] == "phone_not_found"
    assert "reconnect" in resp.json()["message"].lower()


def test_choosing_a_phone_that_waits_for_allow_is_refused(
    ctx: AppContext, client: TestClient
) -> None:
    tracker = start_tracker(ctx, {A: "device", B: "unauthorized"})
    try:
        wait_for(lambda: len(tracker.attached_phones()) == 2)
        ref = next(
            p["ref"] for p in get(client, "devices").json()["phones"] if p["waiting_for_allow"]
        )
        resp = post(client, "phone", {"ref": ref})
        assert resp.status_code == 409 and resp.json()["code"] == "phone_not_ready"
        assert "Allow" in resp.json()["message"]
        assert stored_uuid(ctx) == []
    finally:
        tracker.stop()


def test_after_setup_a_different_phone_cannot_be_chosen_without_switching(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    post(client, "complete")
    resp = post(client, "phone", {"ref": ref_of(client, "Beta 2")})
    assert resp.status_code == 409 and resp.json()["code"] == "phone_already_chosen"
    assert stored_uuid(ctx) == [(A, "Alpha 1")]
    # choosing the stored phone again is harmless
    assert post(client, "phone", {"ref": ref_of(client, "Alpha 1")}).status_code == 200


@pytest.mark.parametrize("typed", [None, "", "yes", "SWITCH PHONE", "switc"])
def test_switch_phone_needs_typed_confirmation(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker, typed: str | None
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    post(client, "complete")
    body: dict[str, Any] = {"ref": ref_of(client, "Beta 2")}
    if typed is not None:
        body["confirmation"] = typed
    resp = post(client, "switch-phone", body)
    assert resp.status_code == 400
    assert resp.json()["code"] == "confirmation_required"
    assert "SWITCH" in resp.json()["message"]
    assert stored_uuid(ctx) == [(A, "Alpha 1")]


def test_switch_phone_with_the_typed_word_changes_the_stored_phone(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    post(client, "complete")
    resp = post(
        client, "switch-phone", {"ref": ref_of(client, "Beta 2"), "confirmation": " switch "}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "Beta 2"
    assert stored_uuid(ctx) == [(A, "Alpha 1"), (B, "Beta 2")]  # the first phone's row stays
    wait_for(lambda: two_phones.status().serial == B)


def test_switch_phone_is_refused_while_a_backup_runs(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    class Snap:
        state = JobState.pulling

    class Jobs:
        def snapshot(self) -> list[Snap]:
            return [Snap()]

    ctx.jobs = Jobs()  # type: ignore[assignment]
    resp = post(client, "switch-phone", {"ref": ref_of(client, "Beta 2"), "confirmation": "SWITCH"})
    assert resp.status_code == 409 and resp.json()["code"] == "job_conflict"
    assert stored_uuid(ctx) == []


# -- the Mac folder ------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def check(client: TestClient, path: str) -> Any:
    return client.get("/api/setup/folder", params={"path": path}, headers=H).json()


def test_a_new_folder_under_home_is_fine_and_shows_free_space(
    client: TestClient, home: Path
) -> None:
    result = check(client, "~/PhoneBackup")
    assert result["ok"] is True and result["problem"] is None
    assert result["path"] == str(home / "PhoneBackup")
    assert result["exists"] is False
    assert isinstance(result["free_bytes"], int) and result["free_bytes"] > 0
    assert not (home / "PhoneBackup").exists()  # looking creates nothing


@pytest.mark.parametrize("path", ["", "   ", "Backups", "relative/dir", "./x"])
def test_mac_root_must_be_a_full_path(client: TestClient, home: Path, path: str) -> None:
    result = check(client, path)
    assert result["ok"] is False
    assert result["problem"]


def test_mac_root_cannot_be_the_disk_or_the_home_folder(client: TestClient, home: Path) -> None:
    assert check(client, "/")["ok"] is False
    assert check(client, "~")["ok"] is False


def test_mac_root_cannot_be_inside_the_app_data_folder(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    for inside in (ctx.paths.support, ctx.paths.support / "stuff", ctx.paths.cache / "x"):
        result = check(client, str(inside))
        assert result["ok"] is False
        assert "own data" in result["problem"]


def test_mac_root_cannot_contain_the_app_data_folder(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    result = check(client, str(ctx.paths.support.parent))
    assert result["ok"] is False and "own data" in result["problem"]


def test_mac_root_cannot_be_a_file_or_an_app(client: TestClient, home: Path) -> None:
    (home / "afile").write_text("x")
    assert "file" in check(client, str(home / "afile"))["problem"]
    assert check(client, str(home / "Thing.app"))["ok"] is False


def test_mac_root_must_be_writable(client: TestClient, home: Path) -> None:
    locked = home / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = check(client, str(locked / "inside"))
        assert result["ok"] is False and "write" in result["problem"]
    finally:
        locked.chmod(0o700)


def test_saving_the_mac_folder_creates_it_the_destination_row_and_the_label(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial, model) VALUES ('u1', 'X1234', 'Alpha 1')")
    resp = post(client, "mac-folder", {"path": "~/PhoneBackup", "label": "My: phone?"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["device_label"] == "My_ phone_"  # made safe for any volume
    assert (home / "PhoneBackup" / "My_ phone_").is_dir()
    with ctx.db.read() as conn:
        dest = conn.execute("SELECT kind, root_path, required FROM destination").fetchall()
        label = conn.execute("SELECT label FROM device").fetchone()[0]
    assert dest == [("mac", str(home / "PhoneBackup"), 1)]
    assert label == "My_ phone_"
    saved, _ = load_config(ctx.paths.config)
    assert saved.backup.mac_root == "~/PhoneBackup"
    assert saved.backup.device_label == "My_ phone_"
    assert ctx.config.backup.device_label == "My_ phone_"


def test_the_label_defaults_to_the_phone_model(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial, model) VALUES ('u1', 'X1234', 'Alpha 1')")
    assert post(client, "mac-folder", {"path": "~/B"}).json()["device_label"] == "Alpha 1"


def test_a_label_that_could_climb_out_is_made_safe(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    resp = post(client, "mac-folder", {"path": "~/B", "label": "../../etc"})
    assert resp.status_code == 200
    assert "/" not in resp.json()["device_label"]
    assert not (home / "etc").exists()


def test_an_invalid_folder_is_422_in_plain_words_and_changes_nothing(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    resp = post(client, "mac-folder", {"path": "relative", "label": "x"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_folder"
    assert "full folder path" in resp.json()["message"]
    assert ctx.config.backup.mac_root == "~/PhoneBackup"
    assert not ctx.paths.config.exists()


def test_the_folder_and_label_cannot_change_once_files_are_backed_up(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    assert post(client, "mac-folder", {"path": "~/B", "label": "Phone"}).status_code == 200
    with ctx.db.tx() as conn:
        dest_id = conn.execute("SELECT id FROM destination").fetchone()[0]
        conn.execute(
            "INSERT INTO content (hash, size, kind, created_at) VALUES ('h', 1, 'other', 1)"
        )
        conn.execute(
            "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at) "
            "VALUES ('h', ?, 'p', 'p', 1, 1)",
            (dest_id,),
        )
    other = post(client, "mac-folder", {"path": "~/C", "label": "Phone"})
    assert other.status_code == 409 and other.json()["code"] == "backups_exist"
    renamed = post(client, "mac-folder", {"path": "~/B", "label": "Other"})
    assert renamed.status_code == 409
    assert post(client, "mac-folder", {"path": "~/B", "label": "Phone"}).status_code == 200


# -- the SSD -------------------------------------------------------------------------------------

SSD = Volume("Backup SSD", "ExFAT", "uuid-ssd", 700, 1000, True, None)
OLD = Volume("Old", "MS-DOS FAT32", "uuid-old", 5, 32, False, "Formatted as FAT32.")


def with_volumes(client: TestClient, *volumes: Volume) -> list[str]:
    calls: list[str] = []

    def lister() -> list[Volume]:
        calls.append("list")
        return list(volumes)

    client.app.dependency_overrides[get_volume_lister] = lambda: lister  # type: ignore[attr-defined]
    return calls


def test_volumes_are_listed_with_their_warnings(client: TestClient) -> None:
    with_volumes(client, SSD, OLD)
    body = get(client, "volumes").json()
    assert [v["name"] for v in body["volumes"]] == ["Backup SSD", "Old"]
    assert body["volumes"][1]["suitable"] is False and "FAT32" in body["volumes"][1]["warning"]
    assert body["selected_uuid"] == ""


def test_the_ssd_choice_is_stored_and_can_be_skipped(client: TestClient, ctx: AppContext) -> None:
    with_volumes(client, SSD, OLD)
    assert post(client, "ssd", {"volume_uuid": "uuid-ssd"}).json()["selected_uuid"] == "uuid-ssd"
    saved, _ = load_config(ctx.paths.config)
    assert saved.backup.ssd_volume_uuid == "uuid-ssd"
    assert post(client, "ssd", {"volume_uuid": None}).json()["selected_uuid"] == ""
    assert load_config(ctx.paths.config)[0].backup.ssd_volume_uuid == ""
    assert post(client, "ssd", {}).status_code == 200  # skipping needs no value


def test_a_drive_that_is_not_there_cannot_be_chosen(client: TestClient, ctx: AppContext) -> None:
    with_volumes(client, SSD)
    resp = post(client, "ssd", {"volume_uuid": "uuid-gone"})
    assert resp.status_code == 422 and resp.json()["code"] == "unknown_volume"
    assert ctx.config.backup.ssd_volume_uuid == ""


def test_a_drive_with_a_poor_file_system_can_still_be_chosen(client: TestClient) -> None:
    with_volumes(client, OLD)
    assert post(client, "ssd", {"volume_uuid": "uuid-old"}).status_code == 200


def test_the_default_volume_lister_is_the_read_only_diskutil_one() -> None:
    from sift.services.volumes import list_external_volumes

    assert get_volume_lister() is list_external_volumes


# -- what to back up -----------------------------------------------------------------------------


@pytest.fixture
def phone_ctx(paths: AppPaths, tmp_path: Path) -> AppContext:
    ctx = make_ctx(paths)
    ctx.config.backup.mac_root = str(tmp_path / "backup")
    phone = build_fake_phone(60)
    phone.remove(BIG_VIDEO)
    transport = FakeTransport(phone)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial) VALUES ('dev-1', 'FAKE-0001')")
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "dev-1")
    ctx.tracker = Tracker(session)  # type: ignore[assignment]
    ctx.jobs = real_jobs(ctx)
    return ctx


def test_the_tree_lists_the_nine_default_sets_before_any_scan(phone_ctx: AppContext) -> None:
    tree = get(client_for(phone_ctx), "sets").json()
    assert tree["phone_connected"] is True and tree["scanning"] is False
    names = [s["name"] for s in tree["sets"]]
    assert len(names) == 9 and names[0] == "Camera" and names[-1] == "Loose files"
    assert all(s["files"] is None and s["bytes"] is None for s in tree["sets"])
    ticked = {s["name"]: s["enabled"] for s in tree["sets"]}
    assert ticked["Downloads"] is False and sum(ticked.values()) == 8
    camera = next(s for s in tree["sets"] if s["name"] == "Camera")
    assert camera["paths"] == ["/sdcard/DCIM"]
    loose = next(s for s in tree["sets"] if s["name"] == "Loose files")
    assert loose["paths"] == ["files directly in /sdcard"]


def test_a_dry_run_scan_gives_sizes_and_copies_nothing(phone_ctx: AppContext) -> None:
    client = client_for(phone_ctx)
    resp = post(client, "scan")
    assert resp.status_code == 200, resp.text
    assert phone_ctx.jobs is not None
    for job in phone_ctx.jobs.snapshot():
        assert phone_ctx.jobs.wait_finished(job.id, 30.0)
    tree = get(client, "sets").json()
    assert tree["scanning"] is False
    camera = next(s for s in tree["sets"] if s["name"] == "Camera")
    assert camera["files"] and camera["files"] > 0
    assert camera["bytes"] and camera["bytes"] > 0
    assert all(s["files"] is not None for s in tree["sets"])  # every set was looked at
    job = phone_ctx.jobs.snapshot()[0]
    assert job.dry_run is True
    root = Path(phone_ctx.config.backup.mac_root)
    assert not root.exists()  # nothing written to the backup folder
    with phone_ctx.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM replica").fetchone()[0] == 0
        states = {r[0] for r in conn.execute("SELECT DISTINCT state FROM phone_file")}
    assert states == {"discovered"}  # nothing was pulled


def test_scanning_needs_a_phone(ctx: AppContext, client: TestClient) -> None:
    ctx.tracker = Tracker(None)  # type: ignore[assignment]
    ctx.jobs = real_jobs(ctx)
    resp = post(client, "scan")
    assert resp.status_code == 409
    assert resp.json()["message"]


def test_ticking_sets_saves_only_their_switch(phone_ctx: AppContext) -> None:
    client = client_for(phone_ctx)
    before = {s.name: (s.paths, s.deletable) for s in phone_ctx.config.backup.sets}
    resp = post(client, "sets", {"enabled": ["camera", "Downloads"]})
    assert resp.status_code == 200, resp.text
    assert {s["name"] for s in resp.json()["sets"] if s["enabled"]} == {"Camera", "Downloads"}
    saved, _ = load_config(phone_ctx.paths.config)
    assert {s.name for s in saved.backup.sets if s.enabled} == {"Camera", "Downloads"}
    assert {s.name: (s.paths, s.deletable) for s in saved.backup.sets} == before


def test_ticking_an_unknown_set_is_422_and_changes_nothing(phone_ctx: AppContext) -> None:
    client = client_for(phone_ctx)
    resp = post(client, "sets", {"enabled": ["Camera", "Nope"]})
    assert resp.status_code == 422 and resp.json()["code"] == "unknown_set"
    assert "Nope" in resp.json()["message"]
    assert sum(s.enabled for s in phone_ctx.config.backup.sets) == 8


def test_at_least_one_set_must_be_ticked(phone_ctx: AppContext) -> None:
    assert post(client_for(phone_ctx), "sets", {"enabled": []}).status_code == 422


# -- the reader test -----------------------------------------------------------------------------


def quick_reader_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def quick(ctx: AppContext, session: DeviceSession) -> Any:
        return run_reader_test(ctx, session, duration_s=0.05)

    monkeypatch.setattr(setup_routes, "run_reader_test", quick)


def test_the_reader_test_stores_its_result(
    phone_ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    quick_reader_test(monkeypatch)
    client = client_for(phone_ctx)
    post(client, "scan")
    assert phone_ctx.jobs is not None
    for job in phone_ctx.jobs.snapshot():
        assert phone_ctx.jobs.wait_finished(job.id, 30.0)
    resp = post(client, "reader-test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tested"] is True and body["readers"] in (1, 2, 4) and body["speed_bps"] > 0
    saved, _ = load_config(phone_ctx.paths.config)
    assert saved.phone.measured_read_bps == body["speed_bps"]
    assert saved.phone.parallel_reads_usb == body["readers"]
    assert not Path(phone_ctx.config.backup.mac_root).exists()  # the bytes were thrown away


def test_the_reader_test_without_a_scan_says_so(
    phone_ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    quick_reader_test(monkeypatch)
    body = post(client_for(phone_ctx), "reader-test").json()
    assert body["tested"] is False and "files" in body["message"]
    assert phone_ctx.config.phone.measured_read_bps == 0


def test_the_reader_test_needs_a_phone(ctx: AppContext, client: TestClient) -> None:
    resp = post(client, "reader-test")
    assert resp.status_code == 409 and resp.json()["code"] == "no_phone"


def test_a_phone_that_drops_during_the_reader_test_is_reported_plainly(
    phone_ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sift.device.transport import DeviceDisconnected

    def boom(ctx: AppContext, session: DeviceSession) -> Any:
        raise DeviceDisconnected("serial SECRET-1234 vanished")

    monkeypatch.setattr(setup_routes, "run_reader_test", boom)
    resp = post(client_for(phone_ctx), "reader-test")
    assert resp.status_code == 502
    assert resp.json()["message"] == "The phone disconnected."
    assert "SECRET" not in resp.text


# -- guarded phone choice and switch -------------------------------------------------------------


class RunningJobs:
    """A job manager stand-in whose one job is not finished."""

    class Snap:
        state = JobState.pulling

    def __init__(self) -> None:
        self.running = True

    def snapshot(self) -> list[Any]:
        return [self.Snap()] if self.running else []


def test_a_stored_phone_is_not_replaced_by_choosing_even_when_setup_is_not_complete(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    """An install upgraded from M1 has a stored phone and no `setup_complete` row."""
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    assert get(client, "state").json()["complete"] is False
    resp = post(client, "phone", {"ref": ref_of(client, "Beta 2")})
    assert resp.status_code == 409
    assert resp.json()["code"] == "phone_already_chosen"
    assert "Switch phone" in resp.json()["message"] and "SWITCH" in resp.json()["message"]
    assert stored_uuid(ctx) == [(A, "Alpha 1")]
    with ctx.db.read() as conn:
        assert conn.execute("SELECT value FROM setting WHERE key = 'active_device'").fetchone()


def test_choosing_the_stored_phone_again_changes_nothing_even_while_a_job_runs(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    ctx.jobs = RunningJobs()  # type: ignore[assignment]
    resp = post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    assert resp.status_code == 200 and resp.json()["model"] == "Alpha 1"


def test_choosing_a_first_phone_is_refused_while_a_job_runs(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    ctx.jobs = RunningJobs()  # type: ignore[assignment]
    resp = post(client, "phone", {"ref": ref_of(client, "Beta 2")})
    assert resp.status_code == 409 and resp.json()["code"] == "job_conflict"
    assert stored_uuid(ctx) == []


@pytest.mark.parametrize("route", ["phone", "switch-phone"])
def test_the_job_check_and_the_change_happen_under_the_lock_that_starts_backups(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker, route: str
) -> None:
    """A backup that starts while the request waits for the lock is seen by the check."""
    if route == "switch-phone":
        post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
        body = {"ref": ref_of(client, "Beta 2"), "confirmation": "SWITCH"}
    else:
        body = {"ref": ref_of(client, "Beta 2")}
    jobs = RunningJobs()
    jobs.running = False
    ctx.jobs = jobs  # type: ignore[assignment]
    answer: list[Any] = []
    with _run_lock:  # what Start and Retry hold while they start a backup
        thread = threading.Thread(target=lambda: answer.append(post(client, route, body)))
        thread.start()
        thread.join(0.3)
        assert thread.is_alive(), "the request must wait for the lock"
        jobs.running = True  # the backup started in the meantime
    thread.join(10)
    assert answer[0].status_code == 409 and answer[0].json()["code"] == "job_conflict"
    assert [row[1] for row in stored_uuid(ctx)] == (["Alpha 1"] if route == "switch-phone" else [])


def test_switch_phone_is_refused_while_a_scan_or_backup_runs_and_nothing_changes(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    ctx.jobs = RunningJobs()  # type: ignore[assignment]
    resp = post(client, "switch-phone", {"ref": ref_of(client, "Beta 2"), "confirmation": "SWITCH"})
    assert resp.status_code == 409 and resp.json()["code"] == "job_conflict"
    assert stored_uuid(ctx) == [(A, "Alpha 1")]


# -- each phone has its own folder ---------------------------------------------------------------


def labels(ctx: AppContext) -> list[tuple[str, str | None]]:
    with ctx.db.read() as conn:
        return [(r[0], r[1]) for r in conn.execute("SELECT model, label FROM device ORDER BY id")]


def test_switching_gives_the_new_phone_its_own_folder_name(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    post(client, "complete")
    resp = post(client, "switch-phone", {"ref": ref_of(client, "Beta 2"), "confirmation": "SWITCH"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["label"] == "Beta 2"
    assert labels(ctx) == [("Alpha 1", None), ("Beta 2", "Beta 2")]  # the old row is untouched
    assert ctx.config.backup.device_label == "Phone"  # the default is not renamed under phone A


def test_the_new_phones_folder_name_never_equals_another_phones_ignoring_case(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    with ctx.db.tx() as conn:
        conn.execute("UPDATE device SET label = 'beta 2'")  # phone A's folder
    resp = post(client, "switch-phone", {"ref": ref_of(client, "Beta 2"), "confirmation": "SWITCH"})
    assert resp.json()["label"] == "Beta 2 2"
    # switching back keeps each phone's own name
    post(client, "switch-phone", {"ref": ref_of(client, "Alpha 1"), "confirmation": "SWITCH"})
    assert labels(ctx) == [("Alpha 1", "beta 2"), ("Beta 2", "Beta 2 2")]


def test_a_phone_with_no_label_of_its_own_collides_with_the_default_name(
    client: TestClient, ctx: AppContext
) -> None:
    tracker = start_tracker(ctx, {A: "device", B: "device"})
    try:
        wait_for(lambda: len(tracker.attached_phones()) == 2)
        post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
        with ctx.db.tx() as conn:  # phone A has been using the default folder name
            conn.execute("UPDATE device SET model = 'Phone'")
        resp = post(
            client, "switch-phone", {"ref": ref_of(client, "Beta 2"), "confirmation": "SWITCH"}
        )
        assert resp.status_code == 200
        assert resp.json()["label"] == "Beta 2"
        tracker.choose_phone(B)
    finally:
        tracker.stop()


def test_the_folder_name_cannot_be_one_another_phone_uses(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO device (uuid, serial, model, label) VALUES ('a', 'A1', 'A', 'Alpha')"
        )
        conn.execute("INSERT INTO device (uuid, serial, model) VALUES ('b', 'B1', 'B')")
        conn.execute("INSERT INTO setting (key, value) VALUES ('active_device', 'b')")
    resp = post(client, "mac-folder", {"path": "~/B", "label": "ALPHA"})
    assert resp.status_code == 409 and resp.json()["code"] == "label_taken"
    assert post(client, "mac-folder", {"path": "~/B", "label": "Beta"}).status_code == 200
    assert labels(ctx) == [("A", "Alpha"), ("B", "Beta")]
    assert ctx.config.backup.device_label == "Phone"  # two phones: the default stays


def done_file(ctx: AppContext, device_uuid: str) -> None:
    with ctx.db.tx() as conn:
        device_id = conn.execute("SELECT id FROM device WHERE uuid = ?", (device_uuid,)).fetchone()[
            0
        ]
        conn.execute(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, present) "
            "VALUES (?, '/sdcard/x', 'camera', 1, 1, 'done', 1)",
            (device_id,),
        )
        dest = conn.execute("SELECT id FROM destination").fetchone()[0]
        conn.execute(
            "INSERT INTO content (hash, size, kind, created_at) VALUES ('h', 1, 'other', 1)"
        )
        conn.execute(
            "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at) "
            "VALUES ('h', ?, 'p', 'p', 1, 1)",
            (dest,),
        )


def test_a_phone_that_was_backed_up_keeps_its_folder_name_but_a_new_one_can_choose(
    client: TestClient, ctx: AppContext, home: Path
) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO device (uuid, serial, model, label) VALUES ('a', 'A1', 'A', 'Alpha')"
        )
    assert post(client, "mac-folder", {"path": "~/B", "label": "Alpha"}).status_code == 200
    done_file(ctx, "a")
    assert post(client, "mac-folder", {"path": "~/B", "label": "Other"}).status_code == 409
    # phone B arrives (switch), has no files yet, so it can still name its folder, and the
    # backup folder itself stays locked
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial, model) VALUES ('b', 'B1', 'Beta')")
        conn.execute("INSERT INTO setting (key, value) VALUES ('active_device', 'b')")
    assert post(client, "mac-folder", {"path": "~/B", "label": "Mine"}).status_code == 200
    assert labels(ctx) == [("A", "Alpha"), ("Beta", "Mine")]
    assert post(client, "mac-folder", {"path": "~/Elsewhere", "label": "Mine"}).status_code == 409


def test_the_state_shows_a_backed_up_phones_folder_name_even_if_it_never_had_its_own(
    client: TestClient, ctx: AppContext
) -> None:
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial, model) VALUES ('a', 'A1', 'Alpha')")
        conn.execute("INSERT INTO destination (kind, root_path) VALUES ('mac', '/x')")
    assert get(client, "state").json()["phone"]["label"] is None  # nothing backed up: free to name
    done_file(ctx, "a")
    assert get(client, "state").json()["phone"]["label"] == "Phone"
    assert get(client, "state").json()["device_label"] == "Phone"


def seeded_phone(salt: str, n: int = 40) -> FakePhone:
    phone = build_fake_phone(n, seed=ord(salt[0]))
    phone.remove(BIG_VIDEO)
    for path, file in list(phone.files.items()):
        if file.size > 100_000 and not file.is_symlink_to:
            phone.modify(path, hashlib.sha256((salt + path).encode()).digest() * 100, file.mtime)
    return phone


def test_after_a_switch_the_new_phones_files_land_in_its_own_folder(
    paths: AppPaths, tmp_path: Path
) -> None:
    ctx = make_ctx(paths)
    root = tmp_path / "backup"
    ctx.config.backup.mac_root = str(root)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial, label) VALUES ('dev-a', 'SER-A', 'Alpha')")
        conn.execute("INSERT INTO device (uuid, serial) VALUES ('dev-b', 'SER-B')")
    transport_a = FakeTransport(seeded_phone("a"))
    session_a = DeviceSession(transport_a, PhoneIdentity.from_info(transport_a.info()), "dev-a")
    tracker = Tracker(session_a)
    ctx.tracker = tracker  # type: ignore[assignment]
    ctx.jobs = real_jobs(ctx)
    client = client_for(ctx)

    def backup() -> None:
        resp = client.post("/api/backup/start", headers=H, json={"sets": ["Camera"]})
        assert resp.status_code == 200, resp.text
        assert ctx.jobs is not None and ctx.jobs.wait_finished(resp.json()["job_id"], 60.0)

    def tree(folder: str) -> set[str]:
        return {str(p.relative_to(root)) for p in (root / folder).rglob("*") if p.is_file()}

    backup()
    before = tree("Alpha")
    assert before and not (root / "Beta").exists()

    # the switch: phone B has its own label, chosen the way Switch phone chooses it
    with ctx.db.tx() as conn:
        conn.execute("UPDATE device SET label = 'Beta' WHERE uuid = 'dev-b'")
    transport_b = FakeTransport(seeded_phone("b"))
    tracker.current = DeviceSession(
        transport_b, PhoneIdentity.from_info(transport_b.info()), "dev-b"
    )
    backup()
    assert tree("Beta")
    assert tree("Alpha") == before  # the old phone's folder is untouched
    assert all(p.startswith("Beta/") for p in tree("Beta"))


def test_a_backed_up_phone_without_a_label_keeps_its_folder_after_switching_away_and_back(
    client: TestClient, ctx: AppContext, two_phones: DeviceTracker
) -> None:
    post(client, "phone", {"ref": ref_of(client, "Alpha 1")})
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO destination (kind, root_path) VALUES ('mac', '/x')")
        a_uuid = conn.execute("SELECT uuid FROM device WHERE model = 'Alpha 1'").fetchone()[0]
    done_file(ctx, a_uuid)  # phone A's files are in the default "Phone" folder
    post(client, "switch-phone", {"ref": ref_of(client, "Beta 2"), "confirmation": "SWITCH"})
    post(client, "switch-phone", {"ref": ref_of(client, "Alpha 1"), "confirmation": "SWITCH"})
    assert labels(ctx) == [("Alpha 1", "Phone"), ("Beta 2", "Beta 2")]
