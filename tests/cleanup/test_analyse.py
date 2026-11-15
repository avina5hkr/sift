"""Analyse phone on the fake phone: four groups, read-only, only its own rows written."""

from sift.backup.adopt import record
from sift.backup.replicator import Dest
from sift.backup.run import mac_target
from sift.cleanup.analyse import analyse, load_analysis, run_analysis
from sift.core.clock import SystemClock
from tests.backup.test_confirm import backed_up, device_id
from tests.backup.test_run import Rig

CAMERA = "/sdcard/DCIM/Camera/"


def counts(rig: Rig) -> dict[str, int]:
    """Row counts of every table, to prove what Analyse wrote."""
    with rig.ctx.db.read() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: conn.execute(f"SELECT count(*) FROM {n}").fetchone()[0] for n in names}


def test_groups_after_a_backup_and_new_files(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    phone = rig.transport.phone
    phone.add_file(CAMERA + "new_1.jpg", b"brand new one", mtime=1_700_000_000)
    found = analyse(rig.ctx.db, rig.ctx.config, rig.transport, ["Camera"]).sets["Camera"]
    total = sum(1 for p in phone.files if p.startswith(CAMERA))
    assert found.not_backed_up.files == 1
    assert found.verified.files == total - 1 and found.verified.bytes > 0
    assert found.problems.files == found.not_verified.files == 0


def test_a_damaged_backup_copy_is_a_problem_not_verified(rig: Rig) -> None:
    from sift.backup.verifier import verify_dest

    backed_up(rig, ["Camera"])
    target = mac_target(rig.ctx.db, rig.ctx.config)
    victim = next(p for p in target.root.rglob("*.jpg"))
    victim.write_bytes(b"\0" * victim.stat().st_size)
    verify_dest(rig.ctx.db, Dest(target.dest_id, "mac", target.root))
    found = analyse(rig.ctx.db, rig.ctx.config, rig.transport, ["Camera"]).sets["Camera"]
    assert found.problems.files == 1


def test_files_matching_unhashed_adopted_files_are_not_verified_yet(rig: Rig) -> None:
    target = mac_target(rig.ctx.db, rig.ctx.config)
    path = next(p for p in rig.transport.phone.files if p.startswith(CAMERA))
    size = rig.transport.phone.files[path].size
    (target.root / "Old").mkdir(parents=True)
    (target.root / "Old/copy.jpg").write_bytes(rig.transport.phone.read_range(path, 0, size))
    record(rig.ctx.db, Dest(target.dest_id, "mac", target.root), SystemClock())
    found = analyse(rig.ctx.db, rig.ctx.config, rig.transport, ["Camera"]).sets["Camera"]
    assert found.not_verified.files >= 1 and found.verified.files == 0


def test_analyse_phone_is_read_only(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    before = counts(rig)
    rig.transport.journal.clear()
    run_id = run_analysis(rig.ctx.db, rig.ctx.config, rig.transport, device_id(rig), ["Camera"])
    assert not any(e.mutating for e in rig.transport.journal)  # nothing done to the phone
    after = counts(rig)
    changed = {t for t in after if after[t] != before[t]}
    assert changed == {"analysis_run"}
    stored = load_analysis(rig.ctx.db, run_id)
    assert stored is not None and stored["state"] == "done"
    assert stored["result"]["sets"]["Camera"]["verified"]["files"] > 0


def test_analyse_over_the_api(rig: Rig) -> None:
    from sift.services.plainwords import describe
    from sift.services.tasks import TaskRunner
    from tests.helpers import H, client_for

    backed_up(rig, ["Camera"])
    rig.ctx.tasks = TaskRunner(rig.ctx, describe)
    client = client_for(rig.ctx)
    assert client.post("/api/analyse/phone", headers=H, json={"sets": ["Nope"]}).status_code == 400
    assert (
        client.post("/api/analyse/phone", headers=H, json={"sets": ["Camera"]}).status_code == 200
    )
    assert rig.ctx.tasks.wait(30)
    run_id = rig.ctx.tasks.current().result["analysis_id"]
    body = client.get(f"/api/analyse/phone/{run_id}", headers=H).json()
    assert body["state"] == "done" and body["result"]["sets"]["Camera"]["verified"]["files"] > 0
    assert client.get("/api/analyse/phone/999", headers=H).status_code == 404


def test_unchecked_files_are_their_own_group_until_hashed(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    seen: list[tuple[int, int, int]] = []

    def partial(found) -> None:  # type: ignore[no-untyped-def]
        cam = found.sets["Camera"]
        seen.append((found.processed_files, cam.unchecked.files, cam.verified.files))

    found = analyse(rig.ctx.db, rig.ctx.config, rig.transport, ["Camera"], on_partial=partial)
    first, last = seen[0], seen[-1]
    assert first[1] > 0 and first[2] == 0  # before any hash: unchecked, never "not backed up"
    assert last[1] == 0 and found.sets["Camera"].unchecked.files == 0
    assert found.processed_files == found.total_files > 0


def test_a_stopped_run_keeps_its_partial_apart_and_names_the_unchecked(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    run_id = run_analysis(  # stopped after the listing, before the first hash
        rig.ctx.db,
        rig.ctx.config,
        rig.transport,
        device_id(rig),
        ["Camera"],
        should_stop=lambda: True,
    )
    stored = load_analysis(rig.ctx.db, run_id)
    assert stored is not None and stored["state"] == "stopped" and stored["result"] is None
    cam = stored["partial"]["sets"]["Camera"]
    assert cam["unchecked"]["files"] > 0 and cam["not_backed_up"]["files"] == 0
    assert stored["processed"] < stored["total"]


def test_latest_keeps_the_complete_result_apart_and_marks_it_stale_after_a_backup(
    rig: Rig,
) -> None:
    from sift.cleanup.analyse import latest_analyses, stale_reason

    backed_up(rig, ["Camera"])
    done_id = run_analysis(rig.ctx.db, rig.ctx.config, rig.transport, device_id(rig), ["Camera"])
    stopped_id = run_analysis(
        rig.ctx.db,
        rig.ctx.config,
        rig.transport,
        device_id(rig),
        ["Camera"],
        should_stop=lambda: True,
    )
    found = latest_analyses(rig.ctx.db, device_id(rig))
    assert found["completed"]["id"] == done_id and found["ended"]["id"] == stopped_id
    assert found["active"] is None
    assert stale_reason(found["completed"], rig.ctx.config, None) is None
    later = (found["completed"]["finished_at"] or 0) + 10
    assert "backup ran after" in (stale_reason(found["completed"], rig.ctx.config, later) or "")
    rig.ctx.config.backup.mac_root = rig.ctx.config.backup.mac_root + "-moved"
    assert "changed" in (stale_reason(found["completed"], rig.ctx.config, None) or "")


def test_coverage_names_the_sets_left_out(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    found = analyse(rig.ctx.db, rig.ctx.config, rig.transport, ["Camera"])
    assert "Camera" not in found.left_out_sets and len(found.left_out_sets) >= 1
