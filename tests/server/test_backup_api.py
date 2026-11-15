"""The backup API over the fake phone: control, summary, failures, the scan guard and the sets."""

import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sift.backup.puller import PullStats
from sift.backup.readers import stored_read_speed
from sift.backup.run import run_backup
from sift.backup.summary import record_summary
from sift.core import config as config_module
from sift.core.clock import SystemClock
from sift.core.config import load_config
from sift.core.context import AppContext
from sift.core.jobs import JobControl, JobManager, JobState
from sift.core.paths import AppPaths
from sift.device.fake_phone import BIG_VIDEO, build_fake_phone
from sift.device.fake_transport import FakeTransport
from sift.device.session import DeviceSession, PhoneIdentity
from sift.services.plainwords import SENTENCES, describe
from tests.helpers import H, client_for, make_ctx


class Tracker:
    def __init__(self, session: DeviceSession | None) -> None:
        self.current = session

    def session(self) -> DeviceSession | None:
        return self.current


@dataclass
class Rig:
    ctx: AppContext
    client: TestClient
    transport: FakeTransport
    root: Path

    def post(self, path: str, body: Any = None) -> Any:
        return self.client.post(f"/api/backup/{path}", headers=H, json=body)

    def get(self, path: str) -> Any:
        return self.client.get(f"/api/backup/{path}", headers=H)

    def wait_job(self, job_id: int, timeout: float = 30.0) -> None:
        assert self.ctx.jobs is not None and self.ctx.jobs.wait_finished(job_id, timeout)

    def run(self, sets: list[str], *, dry_run: bool = False) -> int:
        resp = self.post("start", {"sets": sets, "dry_run": dry_run})
        assert resp.status_code == 200, resp.text
        job_id: int = resp.json()["job_id"]
        self.wait_job(job_id)
        return job_id


def real_jobs(ctx: AppContext) -> JobManager:
    def runner(control: JobControl) -> None:
        run_backup(
            ctx,
            control,
            control.sets,
            control.dry_run,
            sleep=lambda _s: None,
            reconnect_pause_s=0.0,
        )

    return JobManager(
        ctx,
        clock=SystemClock(),
        runner=runner,
        describe=describe,
        stored_speed=lambda: stored_read_speed(ctx),
        sleep=lambda _s: None,
        poll_s=0.01,
    )


@pytest.fixture
def rig(paths: AppPaths, tmp_path: Path) -> Iterator[Rig]:
    ctx = make_ctx(paths)
    root = tmp_path / "backup"
    ctx.config.backup.mac_root = str(root)
    phone = build_fake_phone(60)
    phone.remove(BIG_VIDEO)
    for path, file in list(phone.files.items()):
        if file.size > 100_000:
            phone.modify(path, hashlib.sha256(path.encode()).digest() * 100, file.mtime)
    transport = FakeTransport(phone)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial) VALUES ('dev-1', 'FAKE-0001')")
    session = DeviceSession(transport, PhoneIdentity.from_info(transport.info()), "dev-1")
    ctx.tracker = Tracker(session)  # type: ignore[assignment]
    ctx.jobs = real_jobs(ctx)
    yield Rig(ctx, client_for(ctx), transport, root)


def blocking_jobs(ctx: AppContext) -> tuple[JobManager, threading.Event]:
    """A manager whose job sits at a checkpoint loop until released (for control requests)."""
    release = threading.Event()

    def runner(control: JobControl) -> None:
        control.set_state(JobState.pulling)
        while not release.is_set():
            control.checkpoint()
            time.sleep(0.005)

    jobs = JobManager(
        ctx,
        clock=SystemClock(),
        runner=runner,
        describe=describe,
        sleep=lambda _s: None,
        poll_s=0.01,
    )
    return jobs, release


def wait_for(check: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# -- the token -----------------------------------------------------------------------------------

ROUTES = [
    ("POST", "/api/backup/start"),
    ("POST", "/api/backup/pause"),
    ("POST", "/api/backup/resume"),
    ("POST", "/api/backup/stop"),
    ("GET", "/api/backup/summary"),
    ("GET", "/api/backup/failures"),
    ("POST", "/api/backup/retry"),
    ("GET", "/api/backup/scan/1"),
    ("POST", "/api/backup/scan/1/confirm"),
    ("POST", "/api/backup/scan/1/discard"),
    ("GET", "/api/backup/sets"),
    ("PUT", "/api/backup/sets"),
    ("GET", "/api/backup/overview"),
    ("POST", "/api/backup/open-folder"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_needs_the_token(client: TestClient, method: str, path: str) -> None:
    for headers in ({}, {"X-Sift-Token": "wrong"}):
        resp = client.request(method, path, headers=headers, json={})
        assert resp.status_code == 401
        assert resp.json()["code"] == "bad_token"


def test_without_a_job_manager_a_start_is_a_plain_409(ctx: AppContext) -> None:
    resp = client_for(ctx).post("/api/backup/start", headers=H, json={"sets": ["Camera"]})
    assert resp.status_code == 409 and resp.json()["code"] == "no_jobs"


# -- start ---------------------------------------------------------------------------------------


def test_start_runs_a_backup_and_the_summary_has_its_counts(rig: Rig) -> None:
    assert rig.get("summary").json() is None
    job_id = rig.run(["Screenshots", "Documents"])
    summary = rig.get("summary").json()
    assert summary["job_id"] == job_id and summary["outcome"] == "done"
    assert summary["new_files"] > 0 and summary["failed"] == 0
    assert summary["bytes"] > 0 and summary["error"] is None
    assert summary["sets"] == ["Screenshots", "Documents"]
    assert summary["finished_at"] >= summary["started_at"]
    assert summary["duration_s"] == summary["finished_at"] - summary["started_at"]
    assert rig.ctx.jobs is not None
    snap = next(s for s in rig.ctx.jobs.snapshot() if s.id == job_id)
    assert snap.files_done == summary["new_files"] + summary["duplicates_skipped"] + (
        summary["moved_links"] + summary["changed_files"] + summary["failed"]
    )
    # The same bytes are on disk.
    stored = sum(p.stat().st_size for p in rig.root.rglob("*") if p.is_file())
    assert stored == summary["bytes"]
    # A second run with nothing new leaves a second, empty summary.
    rig.run(["Screenshots", "Documents"])
    again = rig.get("summary").json()
    assert again["job_id"] != job_id and again["new_files"] == 0 and again["bytes"] == 0


def test_start_with_an_unknown_set_is_a_plain_422(rig: Rig) -> None:
    resp = rig.post("start", {"sets": ["Camera", "Nonesuch"]})
    assert resp.status_code == 422 and resp.json()["code"] == "unknown_set"
    assert '"Nonesuch"' in resp.json()["message"] and "settings" in resp.json()["message"]


@pytest.mark.parametrize("body", [{}, {"sets": []}, {"sets": ["Camera"], "dry_run": "yes"}])
def test_start_checks_its_body(rig: Rig, body: dict[str, Any]) -> None:
    assert rig.post("start", body).status_code == 422


def test_start_with_no_phone_is_a_409_in_plain_words(rig: Rig) -> None:
    rig.ctx.tracker.current = None  # type: ignore[union-attr]
    resp = rig.post("start", {"sets": ["Camera"]})
    assert resp.status_code == 409
    assert resp.json()["code"] == "no_phone"
    assert resp.json()["message"] == "Connect your phone to start a backup."


def test_a_dry_run_leaves_no_summary(rig: Rig) -> None:
    rig.run(["Screenshots"], dry_run=True)
    assert rig.get("summary").json() is None


# -- control -------------------------------------------------------------------------------------


def test_a_second_start_while_one_runs_is_409(rig: Rig) -> None:
    jobs, release = blocking_jobs(rig.ctx)
    rig.ctx.jobs = jobs
    try:
        first = rig.post("start", {"sets": ["Camera"]})
        assert first.status_code == 200
        second = rig.post("start", {"sets": ["Camera"]})
        assert second.status_code == 409
        assert second.json() == {
            "code": "job_conflict",
            "message": "A backup is already running.",
            "detail": None,
        }
        assert rig.post("retry").status_code == 409  # nothing failed, and one is running
    finally:
        release.set()
        jobs.stop_all(5)


def test_pause_resume_and_stop_act_on_the_running_backup(rig: Rig) -> None:
    jobs, release = blocking_jobs(rig.ctx)
    rig.ctx.jobs = jobs
    try:
        job_id = rig.post("start", {"sets": ["Camera"]}).json()["job_id"]
        paused = rig.post("pause")
        assert paused.status_code == 200 and paused.json()["pause_requested"] is True
        assert paused.json()["job_id"] == job_id
        wait_for(lambda: jobs.snapshot()[0].state is JobState.paused)
        resumed = rig.post("resume")
        assert resumed.json()["pause_requested"] is False
        wait_for(lambda: jobs.snapshot()[0].state is JobState.pulling)
        stopped = rig.post("stop", {"job_id": job_id})
        assert stopped.status_code == 200
        rig.wait_job(job_id)
        snap = jobs.snapshot()[0]
        assert snap.state is JobState.done and snap.stopped
        # Nothing is running now, so a control request with no job is a plain 409.
        for action in ("pause", "resume", "stop"):
            resp = rig.post(action)
            assert resp.status_code == 409 and resp.json()["code"] == "no_backup_running"
        # Naming the finished job: stop is a no-op, pause and resume are refused.
        assert rig.post("stop", {"job_id": job_id}).status_code == 200
        assert rig.post("pause", {"job_id": job_id}).status_code == 409
        assert rig.post("resume", {"job_id": job_id}).status_code == 409
    finally:
        release.set()
        jobs.stop_all(5)


def test_a_job_that_does_not_exist_is_404(rig: Rig) -> None:
    for action in ("pause", "resume", "stop"):
        resp = rig.post(action, {"job_id": 9999})
        assert resp.status_code == 404 and resp.json()["code"] == "job_not_found"


# -- failures and retry --------------------------------------------------------------------------


def _fail_rows(rig: Rig, count: int, *, error: str | None = "The phone disconnected.") -> list[int]:
    with rig.ctx.db.tx() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM phone_file WHERE state = 'done' AND present = 1 "
                "ORDER BY id LIMIT ?",
                (count,),
            )
        ]
        conn.executemany(
            "UPDATE phone_file SET state = 'failed', error = ?, attempts = 4 WHERE id = ?",
            [(error, i) for i in ids],
        )
    return ids


def test_failures_list_has_plain_reasons_and_display_paths(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    assert rig.get("failures").json() == {"count": 0, "files": []}
    ids = _fail_rows(rig, 2)
    with rig.ctx.db.tx() as conn:
        # A phone name with a byte that is not UTF-8, stored in its NUL-escaped form.
        conn.execute(
            "UPDATE phone_file SET remote_path = ?, error = NULL WHERE id = ?",
            ("/sdcard/Documents/caf\x00233.pdf", ids[1]),
        )
    body = rig.get("failures").json()
    assert body["count"] == 2
    by_id = {f["id"]: f for f in body["files"]}
    assert by_id[ids[0]]["reason"] == "The phone disconnected."
    assert by_id[ids[0]]["attempts"] == 4 and by_id[ids[0]]["size"] > 0
    odd = by_id[ids[1]]
    assert odd["path"] == "/sdcard/Documents/caf%E9.pdf"
    assert odd["reason"] == describe(RuntimeError())  # no stored reason: the generic sentence
    assert "\x00" not in rig.get("failures").text and "\\u0000" not in rig.get("failures").text
    assert all(f["reason"] in SENTENCES for f in body["files"])


def test_failures_leave_out_files_gone_from_the_phone(rig: Rig) -> None:
    rig.run(["Screenshots"])
    (fid,) = _fail_rows(rig, 1)
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE phone_file SET present = 0 WHERE id = ?", (fid,))
    assert rig.get("failures").json()["count"] == 0


def test_retry_puts_failed_files_back_and_starts_a_backup(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    ids = _fail_rows(rig, 3)
    resp = rig.post("retry")
    assert resp.status_code == 200 and resp.json()["retried"] == 3
    rig.wait_job(resp.json()["job_id"])
    with rig.ctx.db.read() as conn:
        states = {
            r[0]: r[1]
            for r in conn.execute(
                f"SELECT id, state FROM phone_file WHERE id IN ({','.join('?' * 3)})", ids
            )
        }
    assert set(states.values()) == {"done"}
    assert rig.get("failures").json()["count"] == 0
    summary = rig.get("summary").json()
    assert summary["job_id"] == resp.json()["job_id"] and summary["outcome"] == "done"


def test_a_normal_backup_does_not_retry_failed_files(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    _fail_rows(rig, 2)
    rig.run(["Screenshots", "Documents"])
    assert rig.get("failures").json()["count"] == 2


def test_retry_with_nothing_failed_is_a_plain_409(rig: Rig) -> None:
    rig.run(["Screenshots"])
    resp = rig.post("retry")
    assert resp.status_code == 409 and resp.json()["code"] == "nothing_to_retry"


def test_retry_while_a_backup_runs_is_409_and_changes_nothing(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    _fail_rows(rig, 2)
    jobs, release = blocking_jobs(rig.ctx)
    rig.ctx.jobs = jobs
    try:
        assert rig.post("start", {"sets": ["Camera"]}).status_code == 200
        resp = rig.post("retry")
        assert resp.status_code == 409 and resp.json()["code"] == "job_conflict"
        assert rig.get("failures").json()["count"] == 2  # still failed, not queued
    finally:
        release.set()
        jobs.stop_all(5)


def test_retry_with_no_phone_puts_the_failures_back_as_they_were(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    _fail_rows(rig, 2)
    session = rig.ctx.tracker.current  # type: ignore[union-attr]
    rig.ctx.tracker.current = None  # type: ignore[union-attr]
    resp = rig.post("retry")
    assert resp.status_code == 409 and resp.json()["code"] == "no_phone"
    assert resp.json()["message"] == "Connect your phone to start a backup."
    assert rig.get("failures").json() == {"count": 0, "files": []}  # no phone, nothing listed
    rig.ctx.tracker.current = session  # type: ignore[union-attr]
    failures = rig.get("failures").json()
    assert failures["count"] == 2
    assert {f["reason"] for f in failures["files"]} == {"The phone disconnected."}
    assert {f["attempts"] for f in failures["files"]} == {4}


# -- the mass-disappearance guard ----------------------------------------------------------------


def _guarded(rig: Rig) -> tuple[int, int]:
    """A second Screenshots run that the guard holds: (job id, scan id)."""
    rig.run(["Screenshots"])
    for path in [p for p in rig.transport.phone.files if "/Screenshots/" in p]:
        rig.transport.phone.remove(path)
    job_id = rig.post("start", {"sets": ["Screenshots"]}).json()["job_id"]
    assert rig.ctx.jobs is not None
    jobs = rig.ctx.jobs
    wait_for(
        lambda: (
            next(s for s in jobs.snapshot() if s.id == job_id).state
            is JobState.awaiting_confirmation
        )
    )
    with rig.ctx.db.read() as conn:
        (scan_id,) = conn.execute(
            "SELECT id FROM scan WHERE state = 'awaiting_confirmation'"
        ).fetchone()
    return job_id, int(scan_id)


def test_the_scan_shows_what_the_guard_saw(rig: Rig) -> None:
    _job_id, scan_id = _guarded(rig)
    scan = rig.get(f"scan/{scan_id}").json()
    assert scan["state"] == "awaiting_confirmation" and scan["sets"] == ["Screenshots"]
    assert scan["disappearance"] and scan["disappearance"][0]["scope"] in ("set", "folder")
    assert any("last time and now shows" in w for w in scan["warnings"])
    assert rig.post(f"scan/{scan_id}/discard").json()["state"] == "discarded"


def test_confirm_applies_the_scan_and_wakes_the_waiting_backup(rig: Rig) -> None:
    job_id, scan_id = _guarded(rig)
    resp = rig.post(f"scan/{scan_id}/confirm")
    assert resp.status_code == 200 and resp.json()["state"] == "applied"
    rig.wait_job(job_id, 10)
    assert rig.ctx.jobs is not None
    snap = next(s for s in rig.ctx.jobs.snapshot() if s.id == job_id)
    assert snap.state is JobState.done and not snap.stopped
    with rig.ctx.db.read() as conn:
        present = conn.execute(
            "SELECT COUNT(*) FROM phone_file WHERE set_name = 'Screenshots' AND present = 1"
        ).fetchone()[0]
    assert present == 0


def test_discard_drops_the_scan_and_wakes_the_waiting_backup(rig: Rig) -> None:
    job_id, scan_id = _guarded(rig)
    resp = rig.post(f"scan/{scan_id}/discard")
    assert resp.status_code == 200 and resp.json()["state"] == "discarded"
    rig.wait_job(job_id, 10)
    with rig.ctx.db.read() as conn:
        present = conn.execute(
            "SELECT COUNT(*) FROM phone_file WHERE set_name = 'Screenshots' AND present = 1"
        ).fetchone()[0]
    assert present > 0  # nothing was applied


def test_answering_a_scan_twice_or_a_missing_scan(rig: Rig) -> None:
    job_id, scan_id = _guarded(rig)
    assert rig.post(f"scan/{scan_id}/confirm").status_code == 200
    rig.wait_job(job_id, 10)
    for action in ("confirm", "discard"):
        resp = rig.post(f"scan/{scan_id}/{action}")
        assert resp.status_code == 409 and resp.json()["code"] == "scan_not_pending"
        assert resp.json()["message"] == "That scan is no longer waiting for your confirmation."
    missing = (rig.get("scan/9999"), rig.post("scan/9999/confirm"), rig.post("scan/9999/discard"))
    for resp in missing:
        assert resp.status_code == 404 and resp.json()["code"] == "scan_not_found"


# -- the sets ------------------------------------------------------------------------------------


def _set(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "paths": ["/sdcard/Pictures/Mine"],
        "shallow_paths": [],
        "exclude": [],
        "enabled": True,
        "deletable": False,
        **fields,
    }


def test_get_sets_lists_the_configured_sets(rig: Rig) -> None:
    sets = rig.get("sets").json()["sets"]
    assert [s["name"] for s in sets] == [s.name for s in rig.ctx.config.backup.sets]
    assert {"name", "paths", "shallow_paths", "exclude", "enabled", "deletable"} <= set(sets[0])


def test_put_sets_validates_saves_and_applies(rig: Rig) -> None:
    new = [_set("Mine", paths=["/storage/emulated/0/Pictures/Mine"]), _set("Second")]
    resp = rig.client.put("/api/backup/sets", headers=H, json={"sets": new})
    assert resp.status_code == 200, resp.text
    assert [s["name"] for s in resp.json()["sets"]] == ["Mine", "Second"]
    assert resp.json()["sets"][0]["paths"] == ["/sdcard/Pictures/Mine"]  # the config's rules
    assert [s.name for s in rig.ctx.config.backup.sets] == ["Mine", "Second"]
    reloaded, warnings = load_config(rig.ctx.paths.config)
    assert [s.name for s in reloaded.backup.sets] == ["Mine", "Second"] and warnings == []
    assert [s["name"] for s in rig.get("sets").json()["sets"]] == ["Mine", "Second"]


@pytest.mark.parametrize(
    ("sets", "words"),
    [
        ([], "at least one"),
        ([_set("A"), _set(" a ")], "unique"),
        ([_set("A", paths=["/etc/passwd"])], "outside /sdcard"),
        ([_set("A", paths=[], shallow_paths=[])], "at least one entry"),
        ([_set("A", exclude=["/sdcard/Other"])], "not inside"),
        ([_set("   ")], "blank"),
        ([_set("A"), "nope"], "Set 2"),
    ],
)
def test_put_sets_refuses_an_invalid_list_in_plain_words(
    rig: Rig, sets: list[Any], words: str
) -> None:
    before = [s.model_dump() for s in rig.ctx.config.backup.sets]
    resp = rig.client.put("/api/backup/sets", headers=H, json={"sets": sets})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_sets" and words in resp.json()["message"]
    assert "Value error" not in resp.json()["message"]
    assert [s.model_dump() for s in rig.ctx.config.backup.sets] == before
    assert not rig.ctx.paths.config.exists()  # nothing was written


def test_put_sets_with_no_body_list_is_refused(rig: Rig) -> None:
    resp = rig.client.put("/api/backup/sets", headers=H, json={"other": 1})
    assert resp.status_code == 422 and resp.json()["code"] == "invalid_sets"


def test_put_sets_that_cannot_be_saved_changes_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_cfg: object, _path: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr("sift.server.routes.backup.save_config", broken)
    before = [s.model_dump() for s in rig.ctx.config.backup.sets]
    resp = rig.client.put("/api/backup/sets", headers=H, json={"sets": [_set("Mine")]})
    assert resp.status_code == 500 and resp.json()["code"] == "save_failed"
    assert [s.model_dump() for s in rig.ctx.config.backup.sets] == before
    assert config_module.save_config is not broken


def test_a_set_chosen_for_a_start_can_be_one_just_added(rig: Rig) -> None:
    rig.client.put("/api/backup/sets", headers=H, json={"sets": [_set("Mine")]})
    assert rig.post("start", {"sets": ["mine"], "dry_run": True}).status_code == 200


# -- the summary row -----------------------------------------------------------------------------


def test_record_summary_survives_a_bad_database_call_without_raising(rig: Rig) -> None:
    rig.ctx.db.close()
    record_summary(
        rig.ctx.db,
        job_id=1,
        device_id=None,
        sets=[],
        outcome="done",
        started_at=1,
        finished_at=2,
        stats=PullStats(),
        warnings=[],
    )


# -- fix round 1 ---------------------------------------------------------------------------------


def test_a_discarded_scan_shows_as_discarded_in_the_summary(rig: Rig) -> None:
    job_id, scan_id = _guarded(rig)
    rig.post(f"scan/{scan_id}/discard")
    rig.wait_job(job_id, 10)
    summary = rig.get("summary").json()
    assert summary["job_id"] == job_id and summary["outcome"] == "discarded"
    assert summary["new_files"] == 0


def test_retry_only_resets_files_of_sets_still_in_the_settings(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    ids = _fail_rows(rig, 3)
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE phone_file SET set_name = 'Removed set' WHERE id = ?", (ids[0],))
    resp = rig.post("retry")
    assert resp.status_code == 200 and resp.json()["retried"] == 2
    rig.wait_job(resp.json()["job_id"])
    failures = rig.get("failures").json()
    assert [f["id"] for f in failures["files"]] == [ids[0]]  # still listed, untouched
    assert failures["files"][0]["attempts"] == 4


def test_retry_with_only_files_of_removed_sets_changes_nothing(rig: Rig) -> None:
    rig.run(["Screenshots"])
    (fid,) = _fail_rows(rig, 1)
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE phone_file SET set_name = 'Removed set' WHERE id = ?", (fid,))
    resp = rig.post("retry")
    assert resp.status_code == 422 and resp.json()["code"] == "unknown_set"
    assert rig.get("failures").json()["count"] == 1


def test_an_unexpected_error_from_start_restores_the_failed_rows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.run(["Screenshots", "Documents"])
    _fail_rows(rig, 2)
    assert rig.ctx.jobs is not None

    def broken(*_a: object, **_k: object) -> int:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(rig.ctx.jobs, "start_backup", broken)
    client = TestClient(rig.client.app, base_url=rig.client.base_url, raise_server_exceptions=False)
    resp = client.post("/api/backup/retry", headers=H)
    assert resp.status_code == 500
    failures = rig.get("failures").json()
    assert failures["count"] == 2 and {f["attempts"] for f in failures["files"]} == {4}


def test_concurrent_retries_start_exactly_one_backup(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    _fail_rows(rig, 3)
    jobs, release = blocking_jobs(rig.ctx)
    rig.ctx.jobs = jobs
    codes: list[int] = []
    barrier = threading.Barrier(4)

    def go() -> None:
        barrier.wait()
        codes.append(rig.client.post("/api/backup/retry", headers=H).status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert sorted(codes) == [200, 409, 409, 409]
        assert len([s for s in jobs.snapshot()]) == 1
    finally:
        release.set()
        jobs.stop_all(5)


# -- overview and open folder --------------------------------------------------------------------


def test_overview_before_any_backup_lists_the_sets_with_no_files(rig: Rig) -> None:
    body = rig.get("overview").json()
    assert [s["name"] for s in body["sets"]] == [s.name for s in rig.ctx.config.backup.sets]
    assert all(s["on_phone"] == s["backed_up"] == s["pending"] == 0 for s in body["sets"])
    assert body["recent"] == [] and body["pending_scan"] is None and body["job"] is None


def test_overview_counts_files_and_lists_the_last_eight(rig: Rig) -> None:
    rig.run(["Screenshots"])
    body = rig.get("overview").json()
    shots = next(s for s in body["sets"] if s["name"] == "Screenshots")
    assert shots["on_phone"] > 0 and shots["backed_up"] == shots["on_phone"]
    assert shots["pending"] == 0
    assert shots["last_copied_at"] is not None  # its newest copy (not a confirmation)
    camera = next(s for s in body["sets"] if s["name"] == "Camera")
    assert camera["last_copied_at"] is None  # nothing of this set was copied
    assert 0 < len(body["recent"]) <= 8
    assert all(f["path"].startswith("/") and f["size"] >= 0 for f in body["recent"])


def test_overview_shows_the_scan_the_guard_holds(rig: Rig) -> None:
    _job_id, scan_id = _guarded(rig)
    held = rig.get("overview").json()["pending_scan"]
    assert held["id"] == scan_id and held["state"] == "awaiting_confirmation"
    assert held["disappearance"]
    rig.post(f"scan/{scan_id}/discard")


def test_open_folder_opens_the_configured_folder_only(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "sift.server.routes.backup.subprocess.run",
        lambda cmd, **_kw: calls.append(cmd),
    )
    rig.root.mkdir(parents=True, exist_ok=True)
    resp = rig.post("open-folder", {"path": "/etc"})  # a body is ignored
    assert resp.status_code == 200
    assert calls == [["open", "--", str(rig.root)]]


def test_open_folder_before_the_folder_exists_is_a_plain_409(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "sift.server.routes.backup.subprocess.run",
        lambda cmd, **_kw: calls.append(cmd),
    )
    resp = rig.post("open-folder")
    assert resp.status_code == 409 and resp.json()["code"] == "no_backup_folder"
    assert calls == []


def test_open_folder_failure_is_plain_words(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **_kw: Any) -> None:
        raise OSError("no open")

    monkeypatch.setattr("sift.server.routes.backup.subprocess.run", boom)
    rig.root.mkdir(parents=True, exist_ok=True)
    resp = rig.post("open-folder")
    assert resp.status_code == 500 and resp.json()["code"] == "open_failed"


def test_overview_names_the_running_backup(rig: Rig) -> None:
    jobs, release = blocking_jobs(rig.ctx)
    rig.ctx.jobs = jobs
    try:
        job_id = rig.post("start", {"sets": ["Screenshots"]}).json()["job_id"]
        job = rig.get("overview").json()["job"]
        assert job["job_id"] == job_id and job["state"] in ("queued", "pulling")
    finally:
        release.set()
        rig.wait_job(job_id)
    assert rig.get("overview").json()["job"] is None


# -- one phone at a time -------------------------------------------------------------------------


def _other_phone(rig: Rig) -> int:
    """A second stored phone with files in a set of the same name, and a scan held for it."""
    with rig.ctx.db.tx() as conn:
        cur = conn.execute("INSERT INTO device (uuid, serial) VALUES ('dev-2', 'FAKE-0002')")
        other = int(cur.lastrowid or 0)
        for i, state in enumerate(("done", "done", "discovered", "failed", "deleted")):
            conn.execute(
                "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, "
                "set_name) VALUES (?, ?, 'camera', 10, 1, ?, 'Screenshots')",
                (other, f"/sdcard/other_{i}.jpg", state),
            )
        cur = conn.execute(
            "INSERT INTO scan (device_id, started_at, sets, state) "
            "VALUES (?, 1, '[\"Screenshots\"]', 'awaiting_confirmation')",
            (other,),
        )
        return int(cur.lastrowid or 0)


def test_overview_counts_only_the_connected_phone(rig: Rig) -> None:
    scan_id = _other_phone(rig)
    body = rig.get("overview").json()
    shots = next(s for s in body["sets"] if s["name"] == "Screenshots")
    assert body["phone_connected"] is True
    assert (shots["on_phone"], shots["backed_up"], shots["pending"]) == (0, 0, 0)
    assert body["pending_scan"] is None  # the held scan is the other phone's
    assert scan_id > 0


def test_overview_with_no_phone_has_no_counts_and_says_so(rig: Rig) -> None:
    _other_phone(rig)
    rig.ctx.tracker = None
    body = rig.get("overview").json()
    assert body["phone_connected"] is False
    assert all(s["on_phone"] == s["backed_up"] == s["pending"] == 0 for s in body["sets"])
    assert body["recent"] == [] and body["pending_scan"] is None


def test_deleted_rows_are_not_on_the_phone(rig: Rig) -> None:
    rig.run(["Screenshots"])
    with rig.ctx.db.tx() as conn:
        conn.execute(
            "UPDATE phone_file SET state = 'deleted' WHERE id = "
            "(SELECT MIN(id) FROM phone_file WHERE set_name = 'Screenshots' AND present = 1)"
        )
    shots = next(s for s in rig.get("overview").json()["sets"] if s["name"] == "Screenshots")
    assert shots["on_phone"] == shots["backed_up"] + shots["pending"]


def test_a_scan_of_another_phone_cannot_be_confirmed_or_discarded(rig: Rig) -> None:
    scan_id = _other_phone(rig)
    for action in ("confirm", "discard"):
        resp = rig.post(f"scan/{scan_id}/{action}")
        assert resp.status_code == 409 and resp.json()["code"] == "wrong_phone"
        assert "different phone" in resp.json()["message"]
    with rig.ctx.db.read() as conn:
        (state,) = conn.execute("SELECT state FROM scan WHERE id = ?", (scan_id,)).fetchone()
    assert state == "awaiting_confirmation"


def test_open_folder_refuses_an_app(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "sift.server.routes.backup.subprocess.run", lambda cmd, **_kw: calls.append(cmd)
    )
    app = tmp_path / "Thing.app"
    app.mkdir()
    for root in (str(app), str(tmp_path / "X.APP")):
        rig.ctx.config.backup.mac_root = root
        (tmp_path / "X.APP").mkdir(exist_ok=True)
        resp = rig.post("open-folder")
        assert resp.status_code == 409 and resp.json()["code"] == "no_backup_folder"
    assert calls == []


# -- final review fixes --------------------------------------------------------------------------


def test_a_changed_file_with_a_name_that_is_not_utf8_leaves_a_summary_that_reads(
    rig: Rig,
) -> None:
    odd = "/sdcard/Pictures/Screenshots/caf\udce9.png"
    rig.transport.phone.add_file(odd, b"y" * 5000, mtime=1_700_000_000)
    rig.transport.faults.size_change_mid_read.add(odd)
    job_id = rig.run(["Screenshots"])
    resp = rig.get("summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id and body["changed_files"] == 1
    assert any("/sdcard/Pictures/Screenshots/caf%E9.png" in w for w in body["warnings"])


def test_a_summary_stored_with_a_raw_phone_path_still_reads(rig: Rig) -> None:
    record_summary(
        rig.ctx.db,
        job_id=1,
        device_id=1,
        sets=["Screenshots"],
        outcome="done",
        started_at=1,
        finished_at=2,
        stats=PullStats(warnings=["/sdcard/caf\udce9.jpg changed", "odd \ud800 one"]),
        warnings=[],
    )
    resp = rig.get("summary")
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == ["/sdcard/caf%E9.jpg changed", "odd \ufffd one"]


def test_a_summary_with_unreadable_warnings_still_reads(rig: Rig) -> None:
    record_summary(
        rig.ctx.db,
        job_id=1,
        device_id=1,
        sets=["Screenshots"],
        outcome="done",
        started_at=1,
        finished_at=2,
        stats=PullStats(),
        warnings=[],
    )
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE run_summary SET warnings = '{not json', sets = '7'")
    resp = rig.get("summary")
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == [] and resp.json()["sets"] == []


def test_failures_retry_and_summary_belong_to_the_connected_phone(rig: Rig) -> None:
    rig.run(["Screenshots", "Documents"])
    ours = _fail_rows(rig, 2)
    other = _other_phone(rig)  # dev-2 has one failed Screenshots file
    assert other > 0
    with rig.ctx.db.read() as conn:
        (theirs,) = conn.execute(
            "SELECT id FROM phone_file WHERE state = 'failed' AND device_id != 1"
        ).fetchone()
        dev2 = int(conn.execute("SELECT id FROM device WHERE uuid = 'dev-2'").fetchone()[0])
    record_summary(
        rig.ctx.db,
        job_id=9999,
        device_id=dev2,
        sets=["Camera"],
        outcome="failed",
        started_at=1,
        finished_at=2,
        stats=PullStats(),
        warnings=[],
    )
    # Phone 1 is connected: only its failures, its summary.
    failures = rig.get("failures").json()
    assert sorted(f["id"] for f in failures["files"]) == sorted(ours)
    assert rig.get("summary").json()["job_id"] != 9999
    resp = rig.post("retry")
    assert resp.status_code == 200 and resp.json()["retried"] == 2
    rig.wait_job(resp.json()["job_id"])
    with rig.ctx.db.read() as conn:
        (state,) = conn.execute("SELECT state FROM phone_file WHERE id = ?", (theirs,)).fetchone()
    assert state == "failed"  # the other phone's row is untouched
    # The other phone connected: its failure and its summary.
    transport = FakeTransport(build_fake_phone(5))
    rig.ctx.tracker.current = DeviceSession(  # type: ignore[union-attr]
        transport, PhoneIdentity.from_info(transport.info()), "dev-2"
    )
    assert [f["id"] for f in rig.get("failures").json()["files"]] == [theirs]
    assert rig.get("summary").json()["job_id"] == 9999
    # No phone: nothing listed, no summary, Retry is a plain 409.
    rig.ctx.tracker.current = None  # type: ignore[union-attr]
    assert rig.get("failures").json() == {"count": 0, "files": []}
    assert rig.get("summary").json() is None
    resp = rig.post("retry")
    assert resp.status_code == 409 and resp.json()["code"] == "no_phone"


def test_a_run_that_fails_in_the_scan_still_belongs_to_its_phone(rig: Rig) -> None:
    def broken(*_a: object, **_k: object) -> list[object]:
        raise RuntimeError("the walk broke")

    rig.transport.walk = broken  # type: ignore[method-assign,assignment]
    job_id = rig.run(["Screenshots"])
    summary = rig.get("summary").json()
    assert summary is not None and summary["job_id"] == job_id and summary["outcome"] == "failed"


def test_the_ticks_under_this_run_are_remembered_and_settings_stay_as_they_were(rig: Rig) -> None:
    sets = rig.ctx.config.backup.sets
    on = next(s for s in sets if s.enabled)
    off = next(s for s in sets if not s.enabled)

    def ticked() -> dict[str, bool]:
        return {s["name"]: s["ticked"] for s in rig.get("overview").json()["sets"]}

    assert ticked() == {s.name: s.enabled for s in sets}  # first time: as in Settings
    assert rig.post("ticks", {"name": on.name, "ticked": False}).status_code == 200
    assert rig.post("ticks", {"name": off.name.upper(), "ticked": True}).status_code == 200
    now = ticked()
    assert now[on.name] is False and now[off.name] is True
    assert on.enabled and not off.enabled  # Settings unchanged
    assert rig.post("ticks", {"name": "Nope", "ticked": True}).status_code == 404
