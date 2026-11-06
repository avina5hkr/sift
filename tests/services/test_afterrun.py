"""What happens when a job ends: the owner is told, and the daily database copy is made."""

from datetime import date
from pathlib import Path
from typing import Any

from sift.app import build_jobs
from sift.core.clock import SystemClock
from sift.core.context import AppContext
from sift.core.dbbackup import copy_name
from sift.core.jobs import JobControl, JobManager, JobRefused, JobSnapshot, JobState
from sift.services.afterrun import after_run, message_for
from sift.services.notify import NullNotifier
from tests.core.test_jobs import FakeTracker

TODAY = date(2026, 11, 7)


class Notifier:
    def __init__(self, *, background: bool = True, allowed: bool = True) -> None:
        self.background, self.allowed = background, allowed
        self.posted: list[tuple[str, str]] = []

    def app_in_background(self) -> bool:
        return self.background

    def post(self, title: str, body: str) -> bool:
        self.posted.append((title, body))
        return self.allowed


def snapshot(**changes: Any) -> JobSnapshot:
    base: dict[str, Any] = dict(
        id=1, type="backup", state=JobState.done, sets=("Camera",), dry_run=False, files_done=0,
        bytes_done=0, files_total=0, bytes_total=0, duplicates_skipped=0, failed=0, current=None,
        speed_bps=None, eta_s=None, eta_basis="reference", pause_requested=False, stopped=False,
        error=None, started_at=1, finished_at=2,
    )  # fmt: skip
    return JobSnapshot(**{**base, **changes})


def summary(**changes: Any) -> dict[str, Any]:
    return {"new_files": 12, "duplicates_skipped": 1, "failed": 0, **changes}


def test_the_message_says_what_was_done_in_plain_words() -> None:
    assert message_for(snapshot(), summary()) == (
        "Backup finished",
        "12 new files backed up, 1 duplicate skipped.",
        "info",
    )
    title, body, level = message_for(snapshot(), summary(new_files=1, failed=2))
    assert (title, level) == ("Backup finished", "warning")
    assert body == "1 new file backed up, 1 duplicate skipped, 2 files failed."
    assert message_for(snapshot(stopped=True), summary())[0] == "Backup stopped"
    failed = snapshot(state=JobState.failed, error="The Mac needs more free space.")
    assert message_for(failed, None) == (
        "Backup did not finish",
        "The Mac needs more free space.",
        "error",
    )


def test_the_app_wires_the_after_run_step_into_its_jobs(ctx: AppContext, monkeypatch: Any) -> None:
    """build_jobs: a job that ends (here: the fake session cannot be scanned) is announced
    through the notifier it was given, and the daily copy is made."""
    ctx.tracker = FakeTracker()  # type: ignore[assignment]
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(ctx.bus, "publish", lambda name, data: events.append((name, data)))
    notifier = Notifier()
    jobs = build_jobs(ctx, notifier)
    job_id = jobs.start_backup(["Camera"])
    assert jobs.wait_finished(job_id, 10)
    assert [t for t, _ in notifier.posted] == ["Backup did not finish"]
    assert [n for n, _ in events if n == "toast"] == []  # a notification was shown instead
    copies = [p.name for p in ctx.paths.db_backups.iterdir() if p.name.startswith("state-daily-")]
    assert len(copies) == 1


def test_denied_permission_falls_back_to_a_toast(ctx: AppContext, monkeypatch: Any) -> None:
    toasts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(ctx.bus, "publish", lambda name, data: toasts.append((name, data)))
    after_run(ctx, snapshot(), Notifier(allowed=False), today=lambda: TODAY)
    assert [n for n, _ in toasts] == ["toast"]
    assert toasts[0][1]["title"] == "Backup finished"
    assert (ctx.paths.db_backups / copy_name(TODAY)).is_file()


def test_a_dry_run_is_neither_announced_nor_copied(ctx: AppContext) -> None:
    notifier = Notifier()
    after_run(ctx, snapshot(dry_run=True), notifier, today=lambda: TODAY)
    assert notifier.posted == []
    assert not (ctx.paths.db_backups / copy_name(TODAY)).exists()


def test_a_failing_copy_does_not_stop_the_announcement(ctx: AppContext, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    object.__setattr__(ctx.paths, "support", blocker)  # db_backups is now under a file
    notifier = Notifier()
    after_run(ctx, snapshot(), notifier, today=lambda: TODAY)  # must not raise
    assert len(notifier.posted) == 1


def test_a_failed_job_is_announced_with_its_reason(ctx: AppContext) -> None:
    ctx.tracker = FakeTracker()  # type: ignore[assignment]
    notifier = Notifier()

    def runner(_c: JobControl) -> None:
        raise JobRefused("The Mac needs more free space.")

    jobs = JobManager(
        ctx,
        clock=SystemClock(),
        runner=runner,
        on_finished=lambda snap: after_run(ctx, snap, notifier, today=lambda: TODAY),
    )
    job_id = jobs.start_backup(["Camera"])
    assert jobs.wait_finished(job_id, 5)
    assert notifier.posted == [("Backup did not finish", "The Mac needs more free space.")]


def test_the_headless_notifier_is_never_a_system_one() -> None:
    assert NullNotifier().post("t", "b") is False
