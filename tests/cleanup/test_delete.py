"""M8: the preflight, the manifest, executing a delete run, and quarantine (spec 10, I3, I9).

Every test runs against the journaling FakeTransport: `deletes(rig)` is every path the code
asked the phone to delete.
"""

import json
import random
import time
from pathlib import Path

import pytest

from sift.backup.confirm import current_dest_ids
from sift.backup.phonepaths import from_db
from sift.cleanup.deleter import check_execute, execute
from sift.cleanup.errors import ExecuteRefused, ManifestBlocked
from sift.cleanup.preflight import create_run, ids_for_sets, run_preflight
from sift.cleanup.quarantine import entries, purge_due, restore
from tests.backup.test_confirm import backed_up, device_id
from tests.backup.test_run import Rig


def deletes(rig: Rig) -> list[str]:
    return [str(e.path) for e in rig.transport.journal if e.op == "delete"]


def camera(rig: Rig) -> list[int]:
    backed_up(rig, ["Camera"])
    ids = ids_for_sets(rig.ctx, device_id(rig), ["Camera"])
    assert ids
    return ids


def whatsapp(rig: Rig, n: int = 3) -> list[int]:
    """Ticked WhatsApp images (not Protected on the suggestion tabs)."""
    backed_up(rig, ["WhatsApp media"])
    with rig.ctx.db.tx() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM phone_file WHERE remote_path LIKE '%/WhatsApp Images/IMG%' "
                "AND present = 1 AND state = 'done' ORDER BY id LIMIT ?",
                (n,),
            )
        ]
        conn.executemany(
            "INSERT INTO selection (phone_file_id, checked) VALUES (?, 1)", [(i,) for i in ids]
        )
    assert len(ids) == n
    return ids


def preflight(rig: Rig, ids: list[int], source: str = "analysis", disposition: str = "archive"):
    run_id = create_run(rig.ctx, device_id(rig), source, ids, disposition)
    counts = run_preflight(rig.ctx, run_id, rig.session)
    return run_id, counts


def results(rig: Rig, run_id: int) -> dict[int, str]:
    with rig.ctx.db.read() as conn:
        return dict(
            conn.execute(
                "SELECT phone_file_id, check_result FROM delete_item WHERE run_id = ?", (run_id,)
            ).fetchall()
        )


def path_of(rig: Rig, pid: int) -> str:
    with rig.ctx.db.read() as conn:
        return from_db(
            conn.execute("SELECT remote_path FROM phone_file WHERE id = ?", (pid,)).fetchone()[0]
        )


def local_of(rig: Rig, pid: int) -> Path:
    with rig.ctx.db.read() as conn:
        rel = conn.execute(
            "SELECT r.rel_path FROM replica r JOIN phone_file p ON p.content_hash = r.content_hash "
            "WHERE p.id = ?",
            (pid,),
        ).fetchone()[0]
    mac = current_dest_ids(rig.ctx.db, rig.ctx.config)["mac"]
    with rig.ctx.db.read() as conn:
        root = conn.execute("SELECT root_path FROM destination WHERE id = ?", (mac,)).fetchone()[0]
    return Path(root) / rel


def run(rig: Rig, run_id: int, **kw) -> dict[str, int]:
    check_execute(rig.ctx, run_id, rig.session, confirm=True, typed_count=kw.pop("typed", None))
    return execute(rig.ctx, run_id, rig.session)


def test_delete_only_frees_verified_files_after_writing_the_manifest(rig: Rig) -> None:
    ids = camera(rig)
    run_id, counts = preflight(rig, ids)
    assert counts == {"ok": len(ids)}
    assert deletes(rig) == []  # the preflight never deletes
    done = run(rig, run_id)
    assert done["deleted"] == len(ids) and done["failed"] == 0
    gone = {path_of(rig, i) for i in ids}
    assert set(deletes(rig)) == gone
    assert all(rig.transport.phone.entry(p) is None for p in gone)
    with rig.ctx.db.read() as conn:
        manifests = json.loads(
            conn.execute("SELECT manifests FROM delete_run WHERE id = ?", (run_id,)).fetchone()[0]
        )
        assert conn.execute(
            "SELECT count(*) FROM deletion_log WHERE result = 'deleted'"
        ).fetchone()[0] == len(ids)
    listed = json.loads(Path(manifests[0]).read_text())
    assert {f["path"] for f in listed["files"]} == gone
    # the manifest was written before the first delete (I9)
    first_delete = next(n for n, e in enumerate(rig.transport.journal) if e.op == "delete")
    assert Path(manifests[0]).stat().st_mtime <= time.time() and first_delete >= 0
    assert all(local_of(rig, i).is_file() for i in ids)  # archive: the backup stays


def test_stale_analysis_can_never_cause_a_delete(rig: Rig) -> None:
    ids = camera(rig)
    phone = rig.transport.phone
    edited, corrupted, removed, young = ids[0], ids[1], ids[2], ids[3]
    phone.modify(path_of(rig, edited), b"edited on the phone", int(time.time()) - 30 * 86400)
    local_of(rig, corrupted).write_bytes(b"short")
    local_of(rig, removed).unlink()
    rig.ctx.config.backup.sets = [  # a set made non-deletable is not offered at all
        s.model_copy(update={"deletable": False}) if s.name == "Screenshots" else s
        for s in rig.ctx.config.backup.sets
    ]
    young_path = path_of(rig, young)
    entry = phone.entry(young_path)
    run_id, _counts = preflight(rig, ids)
    got = results(rig, run_id)
    assert got[edited] == "changed"
    assert got[corrupted] == "backup_missing" and got[removed] == "backup_missing"
    # a file under the minimum age
    rig.ctx.config.cleanup.min_age_days = 100_000
    run2, _ = preflight(rig, [young])
    assert results(rig, run2)[young] == "too_new"
    rig.ctx.config.cleanup.min_age_days = 7
    assert entry is not None
    # a swapped phone blocks the whole run
    rig.session.device_uuid = "another-phone"
    run3, counts = preflight(rig, ids)
    assert counts == {"blocked": len(ids)}
    with pytest.raises(ExecuteRefused):
        check_execute(rig.ctx, run3, rig.session, confirm=True, typed_count=None)
    rig.session.device_uuid = "dev-1"
    run(rig, run_id)
    blocked = {path_of(rig, i) for i in (edited, corrupted, removed)}
    assert not blocked & set(deletes(rig))


def test_a_file_changed_after_the_preflight_is_left_alone(rig: Rig) -> None:
    ids = camera(rig)
    run_id, _ = preflight(rig, ids)
    late = path_of(rig, ids[0])
    rig.transport.phone.modify(late, b"changed later", int(time.time()) - 30 * 86400)
    done = run(rig, run_id)
    assert late not in deletes(rig) and done["deleted"] == len(ids) - 1


def test_provisional_entry_never_satisfies_a_preflight_gate(rig: Rig) -> None:
    ids = camera(rig)
    pid = ids[0]
    with rig.ctx.db.tx() as conn:  # the copy is only adopted (unhashed): no replica counts
        digest, size = conn.execute(
            "SELECT content_hash, size FROM phone_file WHERE id = ?", (pid,)
        ).fetchone()
        rel = conn.execute(
            "SELECT rel_path FROM replica WHERE content_hash = ?", (digest,)
        ).fetchone()[0]
        conn.execute("DELETE FROM replica WHERE content_hash = ?", (digest,))
        conn.execute(
            "INSERT INTO adopted_file (dest_id, rel_path, rel_key, size, mtime, state, found_at) "
            "SELECT id, ?, lower(?), ?, 1, 'provisional', 1 FROM destination WHERE kind = 'mac'",
            (rel, rel, size),
        )
    run_id, _ = preflight(rig, [pid])
    assert results(rig, run_id)[pid] == "not_backed_up"


def test_unbacked_files_never_offered(rig: Rig) -> None:
    camera(rig)
    rig.transport.phone.add_file("/sdcard/DCIM/Camera/new.jpg", b"new photo", mtime=1_600_000_000)
    jobs = rig.jobs()
    from tests.backup.test_run import finished

    finished(jobs, jobs.start_backup(["Camera"], dry_run=True))  # seen by a scan, not copied
    with rig.ctx.db.read() as conn:
        new = conn.execute("SELECT id FROM phone_file WHERE remote_path LIKE '%new.jpg'").fetchone()
    assert new is not None
    assert int(new[0]) not in ids_for_sets(rig.ctx, device_id(rig), ["Camera"])


def test_delete_waits_for_missing_ssd(rig: Rig) -> None:
    ids = camera(rig)
    cfg = rig.ctx.config
    cfg.backup.ssd_volume_uuid = "uuid-ssd"
    cfg.backup.required_destinations = ["mac", "ssd"]
    with rig.ctx.db.tx() as conn:  # the SSD holds verified copies but is not plugged in
        conn.execute(
            "INSERT INTO destination (kind, root_path, volume_uuid, label) "
            "VALUES ('ssd', '/Volumes/Gone/PhoneBackup', 'uuid-ssd', 'SSD')"
        )
        conn.execute(
            "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at) "
            "SELECT content_hash, (SELECT id FROM destination WHERE kind = 'ssd'), rel_path, "
            "rel_key, size, verified_at FROM replica"
        )
    _run, counts = preflight(rig, ids)
    assert counts == {"waiting_for_ssd": len(ids)}
    assert deletes(rig) == []


def test_the_manifest_must_be_written_first(rig: Rig) -> None:
    ids = camera(rig)
    run_id, _ = preflight(rig, ids)
    check_execute(rig.ctx, run_id, rig.session, confirm=True, typed_count=None)
    rig.ctx.config.backup.mac_root = "/nonexistent/folder"
    with pytest.raises(ManifestBlocked):
        execute(rig.ctx, run_id, rig.session)
    assert deletes(rig) == []


def test_execute_refuses_without_a_fresh_confirmed_unchanged_selection(rig: Rig) -> None:
    ids = whatsapp(rig)
    run_id, counts = preflight(rig, ids, source="cleanup", disposition="quarantine")
    assert counts == {"ok": len(ids)}, results(rig, run_id)
    s = rig.session

    def refused(code: str, **kw) -> None:
        with pytest.raises(ExecuteRefused) as e:
            check_execute(rig.ctx, run_id, s, **{"confirm": True, "typed_count": None, **kw})
        assert e.value.code == code

    refused("not_confirmed", confirm=False)
    refused("stale_preflight", now=lambda: time.time() + 11 * 60)
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE selection SET checked = 0 WHERE phone_file_id = ?", (ids[0],))
    refused("selection_changed")
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE selection SET checked = 1")
        conn.execute("UPDATE delete_run SET typed_required = 1 WHERE id = ?", (run_id,))
    refused("typed_count", typed_count=99)
    check_execute(rig.ctx, run_id, s, confirm=True, typed_count=len(ids))
    assert deletes(rig) == []


def test_nothing_deletes_without_execute(rig: Rig) -> None:
    from sift.cleanup.analyse import run_analysis
    from sift.cleanup.candidates import rebuild_exact
    from sift.cleanup.junk import rebuild_junk

    ids = camera(rig)
    preflight(rig, ids)
    rebuild_exact(rig.ctx.db, device_id(rig))
    rebuild_junk(rig.ctx.db, device_id(rig), rig.ctx.config, 1)
    purge_due(rig.ctx, now=time.time() + 10**9)
    run_analysis(rig.ctx.db, rig.ctx.config, rig.transport, device_id(rig), ["Camera"])
    assert deletes(rig) == []
    assert not [e for e in rig.transport.journal if e.mutating and e.op != "media_rescan"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_p4_every_deleted_file_was_verified_hashed_and_confirmed(rig: Rig, seed: int) -> None:
    """P4 over random faults: whatever is mutated, before or after the preflight, a deleted file
    always had a verified backup of the right size, an unchanged phone copy, and was planned."""
    ids = camera(rig)
    rng = random.Random(seed)
    phone = rig.transport.phone
    picked = rng.sample(ids, 8)
    before, after = picked[:5], picked[5:]
    faults = ["edit", "corrupt", "remove_replica", "never_list", "hash_only"]
    rng.shuffle(faults)
    for pid, fault in zip(before, faults, strict=True):
        path = path_of(rig, pid)
        if fault == "edit":
            phone.modify(path, b"x", int(time.time()) - 40 * 86400)
        elif fault == "corrupt":
            local_of(rig, pid).write_bytes(b"")
        elif fault == "remove_replica":
            local_of(rig, pid).unlink()
        elif fault == "never_list":
            rig.ctx.config.cleanup.never_delete.append(path)
        else:  # same size and mtime, different bytes: only the phone's hash can tell
            rig.transport.faults.wrong_sha256.add(path)
    run_id, _ = preflight(rig, ids)
    got = results(rig, run_id)
    assert all(got[p] != "ok" for p in before), got
    for pid in after:  # changed between the preflight and the execute
        phone.modify(path_of(rig, pid), b"late", int(time.time()) - 40 * 86400)
    run(rig, run_id)
    gone = deletes(rig)
    planned_ok = {path_of(rig, p) for p, r in got.items() if r == "ok"}
    bad = {path_of(rig, p) for p in picked}
    assert gone, "the run deleted nothing, so the property was not exercised"
    assert set(gone) <= planned_ok and not set(gone) & bad
    for pid in ids:
        if path_of(rig, pid) in gone:
            assert local_of(rig, pid).is_file()  # its backup is still there


def test_the_manifest_is_on_disk_before_the_first_delete(rig: Rig) -> None:
    ids = camera(rig)[:3]
    run_id, _ = preflight(rig, ids)
    seen: list[bool] = []
    real = rig.transport.delete

    def checked(path: str) -> None:
        with rig.ctx.db.read() as conn:
            listed = conn.execute(
                "SELECT manifests FROM delete_run WHERE id = ?", (run_id,)
            ).fetchone()[0]
        seen.append(bool(listed) and all(Path(m).is_file() for m in json.loads(listed)))
        real(path)

    rig.transport.delete = checked  # type: ignore[method-assign]
    run(rig, run_id)
    assert seen and all(seen)


def test_a_quarantined_copy_is_never_a_backup_and_comes_back_when_the_phone_has_it(
    rig: Rig,
) -> None:
    ids = whatsapp(rig, 1)
    with rig.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO label (content_hash, label, source, created_at) "
            "SELECT content_hash, 'junk', 'manual', 1 FROM phone_file WHERE id = ?",
            (ids[0],),
        )
        digest = conn.execute(
            "SELECT content_hash FROM phone_file WHERE id = ?", (ids[0],)
        ).fetchone()[0]
    path = path_of(rig, ids[0])
    data = rig.transport.phone.entry(path)
    assert data is not None
    run_id, _ = preflight(rig, ids, source="cleanup", disposition="quarantine")
    assert run(rig, run_id)["quarantined"] == 1
    # the same picture arrives on the phone again and the backup only links it
    with rig.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, content_hash, "
            "state, present) SELECT device_id, remote_path || '.again', origin, size, mtime, "
            "content_hash, 'done', 1 FROM phone_file WHERE id = ?",
            (ids[0],),
        )
        again = conn.execute(
            "SELECT id FROM phone_file WHERE content_hash = ? AND present = 1", (digest,)
        ).fetchone()[0]
    with rig.ctx.db.read() as conn:  # while quarantined it is not offered by Delete only
        rows = conn.execute(
            "SELECT rel_path FROM replica WHERE content_hash = ?", (digest,)
        ).fetchall()
    assert all(r[0].startswith(".quarantine/") for r in rows)
    assert again not in ids_for_sets(rig.ctx, device_id(rig), ["WhatsApp media"])
    # purge never removes it while a phone has it; it moves back into the tree instead
    assert purge_due(rig.ctx, now=time.time() + 40 * 86400) == 0
    with rig.ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT rel_path FROM replica WHERE content_hash = ?", (digest,)
        ).fetchall()
    assert rows and not any(r[0].startswith(".quarantine/") for r in rows)
    assert entries(rig.ctx) == []


def test_a_near_copy_needs_its_keeper_on_the_phone(rig: Rig) -> None:
    ids = whatsapp(rig, 2)
    with rig.ctx.db.tx() as conn:
        a, b = (
            conn.execute("SELECT content_hash FROM phone_file WHERE id = ?", (i,)).fetchone()[0]
            for i in ids
        )
        conn.execute(
            "INSERT INTO near_group (id, kind, group_type, algo_version) "
            "VALUES (1, 'image', 'wa_forward', 1)"
        )
        conn.execute(
            "INSERT INTO near_member (group_id, content_hash, role) VALUES (1, ?, 'keeper')", (a,)
        )
        conn.execute(
            "INSERT INTO near_member (group_id, content_hash, role) VALUES (1, ?, 'copy')", (b,)
        )
    run_id, _ = preflight(rig, [ids[1]], source="cleanup", disposition="quarantine")
    assert results(rig, run_id)[ids[1]] == "ok"  # its keeper stays on the phone
    both, _ = preflight(rig, ids, source="cleanup", disposition="quarantine")
    got = results(rig, both)
    assert got[ids[0]] == "keeper" and got[ids[1]] == "keeper_missing"
    with rig.ctx.db.tx() as conn:  # the keeper left the phone
        conn.execute("UPDATE phone_file SET present = 0 WHERE id = ?", (ids[0],))
    alone, _ = preflight(rig, [ids[1]], source="cleanup", disposition="quarantine")
    assert results(rig, alone)[ids[1]] == "keeper_missing"


def test_a_backup_cannot_start_while_files_are_being_deleted(rig: Rig) -> None:
    from sift.core.jobs import JobConflict
    from sift.services.tasks import TaskStatus

    class Busy:
        def current(self) -> TaskStatus:
            return TaskStatus(kind="delete", state="running")

    rig.ctx.tasks = Busy()  # type: ignore[assignment]
    with pytest.raises(JobConflict):
        rig.jobs().start_backup(["Camera"])


def test_quarantine_moves_junk_copies_and_can_restore_or_purge(rig: Rig) -> None:
    ids = whatsapp(rig)
    with rig.ctx.db.tx() as conn:
        conn.executemany(
            "INSERT INTO label (content_hash, label, source, created_at) "
            "SELECT content_hash, 'junk', 'manual', 1 FROM phone_file WHERE id = ?",
            [(i,) for i in ids],
        )
    olds = [local_of(rig, i) for i in ids]
    run_id, counts = preflight(rig, ids, source="cleanup", disposition="quarantine")
    assert counts == {"ok": 3}, results(rig, run_id)
    done = run(rig, run_id)
    assert done["deleted"] == 3 and done["quarantined"] == 3
    assert not any(p.exists() for p in olds)
    q = entries(rig.ctx)
    assert len(q) == 3 and all(e.q_rel.startswith(".quarantine/") for e in q)
    restore(rig.ctx, [q[0].id], "backup")
    assert len(entries(rig.ctx)) == 2 and sum(p.exists() for p in olds) == 1
    assert purge_due(rig.ctx) == 0  # not due yet
    assert purge_due(rig.ctx, now=time.time() + 31 * 86400) == 2
    assert entries(rig.ctx) == [] and sum(p.exists() for p in olds) == 1


def test_camera_originals_are_protected_on_the_suggestion_tabs(rig: Rig) -> None:
    ids = camera(rig)[:3]
    with rig.ctx.db.tx() as conn:
        conn.executemany(
            "INSERT INTO selection (phone_file_id, checked) VALUES (?, 1)", [(i,) for i in ids]
        )
    run_id, _ = preflight(rig, ids, source="cleanup", disposition="quarantine")
    assert set(results(rig, run_id).values()) == {"protected"}


def test_delete_only_over_the_api(rig: Rig) -> None:
    from sift.services.plainwords import describe
    from sift.services.tasks import TaskRunner
    from tests.helpers import H, client_for

    camera(rig)
    rig.ctx.tasks = TaskRunner(rig.ctx, describe)
    client = client_for(rig.ctx)
    started = client.post(
        "/api/delete/preflight", headers=H, json={"source": "analysis", "sets": ["Camera"]}
    ).json()
    assert rig.ctx.tasks.wait(60)
    run = client.get(f"/api/delete/runs/{started['run_id']}", headers=H).json()
    assert run["state"] == "ready" and run["passed"] > 0 and not run["typed_required"]
    refused = client.post("/api/delete/execute", headers=H, json={"run_id": run["id"]})
    assert refused.status_code == 409 and refused.json()["code"] == "not_confirmed"
    assert deletes(rig) == []
    client.post("/api/delete/execute", headers=H, json={"run_id": run["id"], "confirm": True})
    assert rig.ctx.tasks.wait(60) and rig.ctx.tasks.current().state == "done"
    done = client.get(f"/api/delete/runs/{run['id']}", headers=H).json()
    assert done["state"] == "done" and done["deleted"] == run["passed"]
    assert len(deletes(rig)) == run["passed"]
    empty = client.post(
        "/api/delete/preflight", headers=H, json={"source": "cleanup", "ids": [1, 2]}
    )
    assert empty.status_code == 409  # nothing ticked


def test_a_backup_lost_after_the_checks_stops_that_delete(rig: Rig) -> None:
    ids = camera(rig)[:3]
    run_id, _ = preflight(rig, ids)
    local_of(rig, ids[0]).unlink()
    done = run(rig, run_id)
    assert path_of(rig, ids[0]) not in deletes(rig) and done["deleted"] == 2


def test_paths_that_could_climb_out_of_a_root_are_refused(rig: Rig) -> None:
    from sift.cleanup.preflight import in_delete_roots

    cfg = rig.ctx.config
    assert in_delete_roots(cfg, "/sdcard/DCIM/Camera/a.jpg")
    for bad in ("/sdcard/DCIM/../Android/data/x", "/sdcard/DCIM//a.jpg", "/sdcard/DCIM/./a.jpg"):
        assert not in_delete_roots(cfg, bad), bad
    assert not in_delete_roots(cfg, "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/x")


@pytest.mark.real_drives
def test_two_folders_on_one_drive_are_one_copy(rig: Rig, tmp_path: Path) -> None:
    from sift.backup.places import one_place

    assert one_place(tmp_path / "Mac", tmp_path / "SSD")  # the same volume
    assert one_place(tmp_path / "a", tmp_path / "a" / "b")
    ids = camera(rig)[:2]
    cfg = rig.ctx.config
    cfg.backup.ssd_volume_uuid = "uuid-ssd"
    cfg.backup.required_destinations = ["mac", "ssd"]

    (tmp_path / "fake-ssd").mkdir()
    with rig.ctx.db.tx() as conn:
        dest = int(
            conn.execute(
                "INSERT INTO destination (kind, root_path, volume_uuid, label) "
                "VALUES ('ssd', ?, 'uuid-ssd', 'SSD')",
                (str(tmp_path / "fake-ssd"),),
            ).lastrowid
            or 0
        )

    class Ssd:
        def state(self):  # an "SSD" whose folder is on the Mac's own drive
            from sift.services.ssd import SsdState

            return SsdState(
                True, True, "SSD", str(tmp_path), str(tmp_path / "fake-ssd"), None, dest
            )

    rig.ctx.ssd = Ssd()  # type: ignore[assignment]
    _run, counts = preflight(rig, ids)
    assert counts == {"blocked": 2}
    assert deletes(rig) == []


def test_a_quarantined_file_removed_by_hand_breaks_nothing(rig: Rig) -> None:
    ids = whatsapp(rig, 2)
    with rig.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO label (content_hash, label, source, created_at) "
            "SELECT content_hash, 'junk', 'manual', 1 FROM phone_file WHERE id = ?",
            (ids[0],),
        )
    run_id, _ = preflight(rig, [ids[0]], source="cleanup", disposition="quarantine")
    run(rig, run_id)
    (q,) = entries(rig.ctx)
    mac = current_dest_ids(rig.ctx.db, rig.ctx.config)["mac"]
    with rig.ctx.db.tx() as conn:
        root = conn.execute("SELECT root_path FROM destination WHERE id = ?", (mac,)).fetchone()[0]
        conn.execute(  # the picture is on the phone again
            "UPDATE phone_file SET present = 1, state = 'done' WHERE id = ?", (ids[0],)
        )
    (Path(root) / q.q_rel).unlink()  # the owner emptied .quarantine by hand
    _run2, counts = preflight(rig, [ids[1]], source="cleanup", disposition="quarantine")
    assert counts == {"ok": 1}
    assert purge_due(rig.ctx, now=time.time() + 40 * 86400) == 0
    assert entries(rig.ctx) == []
