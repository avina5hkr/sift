"""One status line for the last backup: an incomplete SSD copy is never complete, never failed."""

from typing import Any

import pytest

from sift.backup.outcome import backup_outcome


def summary(outcome: str = "done", **ssd: Any) -> dict[str, Any]:
    return {"outcome": outcome, "finished_at": 100, "error": None, "ssd": ssd or None}


def test_no_backup_yet() -> None:
    assert backup_outcome(None, None, ssd_chosen=False).level == "none"


def test_complete_on_both_only_when_the_ssd_lacks_nothing_now() -> None:
    assert backup_outcome(summary(), 0, ssd_chosen=True).headline == "Complete: Mac and SSD"
    assert backup_outcome(summary(), None, ssd_chosen=False).headline == "Complete"


@pytest.mark.parametrize("stored", ["complete", "incomplete", "none"])
def test_ssd_pending_whatever_the_run_stored_while_files_are_missing_now(stored: str) -> None:
    out = backup_outcome(summary(state=stored, reason=None), 120, ssd_chosen=True)
    assert out.level == "ssd_pending" and out.headline == "Mac complete · SSD: 120 files pending"
    assert "fail" not in out.headline.lower() and "Complete:" not in out.headline


def test_an_unplugged_drive_says_so() -> None:
    out = backup_outcome(
        summary(state="incomplete", reason="The SSD was not connected."), 1, ssd_chosen=True
    )
    assert out.headline == "Mac complete · SSD: 1 file pending (drive unplugged)"


def test_a_later_copy_makes_it_complete() -> None:
    s = summary(state="incomplete", reason="The SSD was not connected.")
    assert backup_outcome(s, 0, ssd_chosen=True).level == "complete"


def test_stopped_and_failed_runs_keep_their_own_words() -> None:
    assert backup_outcome(summary("stopped"), 5, ssd_chosen=True).headline == (
        "Stopped · SSD: 5 files pending"
    )
    failed = {**summary("failed"), "error": "The phone disconnected."}
    assert backup_outcome(failed, 0, ssd_chosen=True).headline == "Failed: The phone disconnected."


def test_the_end_of_run_message_names_the_ssd_pending() -> None:
    from sift.core.jobs import JobSnapshot, JobState
    from sift.services.afterrun import message_for

    snap = JobSnapshot(
        1,
        "backup",
        JobState.done,
        ("Camera",),
        False,
        3,
        30,
        3,
        30,
        0,
        0,
        None,
        None,
        None,
        "reference",
        False,
        False,
        None,
        1,
        2,
    )
    s = {
        "outcome": "done",
        "new_files": 3,
        "duplicates_skipped": 0,
        "failed": 0,
        "ssd": {"state": "incomplete", "pending_after": 120},
    }
    title, body, level = message_for(snap, s)
    assert title == "Backup finished" and "SSD: 120 files pending" in body and level == "warning"
