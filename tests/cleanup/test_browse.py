"""M11: Browse and delete, empty folders, the Archive browser; P8 and P9."""

import json
import time
from pathlib import Path

import pytest

from sift.cleanup.browse import FileRow, smart_lists
from sift.cleanup.deleter import check_execute, execute
from sift.cleanup.preflight import create_run, run_preflight
from sift.core.config import Config
from tests.backup.test_confirm import backed_up, device_id
from tests.backup.test_run import Rig
from tests.cleanup.test_delete import deletes, local_of, path_of, results


def client(rig: Rig):
    from sift.services.plainwords import describe
    from sift.services.tasks import TaskRunner
    from tests.helpers import H, client_for

    rig.ctx.tasks = TaskRunner(rig.ctx, describe)
    return client_for(rig.ctx), H


def browse_preflight(rig: Rig, c, h, **body) -> dict:
    started = c.post("/api/delete/preflight", headers=h, json={"source": "browse", **body})
    assert started.status_code == 200, started.json()
    assert rig.ctx.tasks.wait(60)
    run = c.get(f"/api/delete/runs/{started.json()['run_id']}", headers=h).json()
    run["skipped"] = started.json()["skipped"]
    return run


def test_browse_shows_folders_sizes_and_backup_status(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    c, h = client(rig)
    top = c.get("/api/browse?path=/sdcard", headers=h).json()
    names = {f["name"]: f for f in top["folders"]}
    assert "DCIM" in names and names["DCIM"]["files"] > 0 and names["DCIM"]["size"] > 0
    camera = c.get("/api/browse?path=/sdcard/DCIM/Camera&sort=name", headers=h).json()
    assert camera["files"] and {f["status"] for f in camera["files"]} == {"mac_only"}
    assert camera["crumbs"] == ["/sdcard", "/sdcard/DCIM", "/sdcard/DCIM/Camera"]
    found = c.get("/api/browse?path=/sdcard/DCIM/Camera&q=.mp4", headers=h).json()
    assert all(f["name"].endswith(".mp4") for f in found["files"])
    assert c.get("/api/browse?path=/etc", headers=h).status_code == 422
    assert {x["key"] for x in c.get("/api/browse/lists", headers=h).json()} >= {"installers"}


def test_smart_lists_pick_the_right_files() -> None:
    lists = smart_lists(Config())
    now = time.time()
    old = int(now - 400 * 86400)

    def row(path: str, kind: str = "other", size: int = 10, mtime: int = old) -> FileRow:
        return FileRow(1, path, size, mtime, "other", kind, "mac_only", None)

    assert lists["installers"][1](row("/sdcard/Download/app.apk"), now)
    assert lists["large_videos"][1](row("/sdcard/Movies/a.mp4", "video", 200_000_000), now)
    assert not lists["large_videos"][1](row("/sdcard/Movies/a.mp4", "video", 5), now)
    assert lists["old_downloads"][1](row("/sdcard/Download/x.pdf"), now)
    assert not lists["old_downloads"][1](row("/sdcard/Download/x.pdf", mtime=int(now)), now)
    assert lists["temp_cache"][1](row("/sdcard/Pictures/.thumbnails/1.jpg"), now)
    wa = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Voice Notes/a.opus"
    assert lists["whatsapp_media"][1](row(wa), now)


def test_a_folder_selection_skips_unbacked_files_and_removes_empty_folders(rig: Rig) -> None:
    backed_up(rig, ["Screenshots"])
    phone = rig.transport.phone
    phone.add_file("/sdcard/DCIM/Screenshots/new-unbacked.png", b"new", mtime=1_600_000_000)
    from tests.backup.test_run import finished

    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Screenshots"], dry_run=True))
    c, h = client(rig)
    run = browse_preflight(rig, c, h, folders=["/sdcard/DCIM/Screenshots"])
    assert run["skipped"] == ["/sdcard/DCIM/Screenshots/new-unbacked.png"]
    assert run["state"] == "ready" and run["passed"] > 0
    assert run["typed_required"] is False  # inside the delete roots, under 200 files
    done = c.post(
        "/api/delete/execute",
        headers=h,
        json={"run_id": run["id"], "confirm": True, "disposition": "archive"},
    )
    assert done.status_code == 200 and rig.ctx.tasks.wait(60)
    assert rig.ctx.tasks.current().state == "done", rig.ctx.tasks.current()
    assert len(deletes(rig)) == run["passed"]
    # the unbacked file keeps the folder alive; nothing standard is ever removed
    assert phone.entry("/sdcard/DCIM/Screenshots") is not None
    assert not [e for e in rig.transport.journal if e.op == "remove_dir"]


def test_empty_folders_are_removed_but_never_a_standard_one(rig: Rig) -> None:
    from sift.cleanup.deleter import remove_empty_folders

    phone = rig.transport.phone
    phone.add_file("/sdcard/Pictures/Trip/Day1/a.jpg", b"a", mtime=1_600_000_000)
    phone.remove("/sdcard/Pictures/Trip/Day1/a.jpg")
    assert remove_empty_folders(rig.transport, ["/sdcard/Pictures/Trip/Day1/a.jpg"]) == 2
    assert phone.entry("/sdcard/Pictures/Trip") is None
    assert phone.entry("/sdcard/Pictures") is not None  # a standard folder stays


def test_outside_the_delete_roots_or_over_200_needs_the_typed_count(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    c, h = client(rig)
    with rig.ctx.db.read() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM phone_file WHERE remote_path LIKE '%/DCIM/Camera/%' AND present = 1"
            )
        ]
    rig.ctx.config.cleanup.delete_roots = ["/sdcard/Pictures"]  # Camera is outside them now
    run = browse_preflight(rig, c, h, ids=ids[:3])
    assert run["passed"] == 3 and run["typed_required"] is True


def test_the_never_delete_list_wins_in_browse(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    with rig.ctx.db.read() as conn:
        pid = conn.execute(
            "SELECT id FROM phone_file WHERE remote_path LIKE '%/DCIM/Camera/%' LIMIT 1"
        ).fetchone()[0]
    rig.ctx.config.browse.never_delete_extra = ["/sdcard/DCIM/Camera"]
    run_id = create_run(rig.ctx, device_id(rig), "browse", [pid], "quarantine")
    run_preflight(rig.ctx, run_id, rig.session)
    assert results(rig, run_id)[pid] == "outside_roots"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_p8_faults_through_the_browse_path(rig: Rig, seed: int) -> None:
    """P4's fault cases through Browse: a folder selection, random faults before and after the
    checks; whatever was deleted had a verified backup, an unchanged phone copy, and was shown."""
    import random

    backed_up(rig, ["Camera"])
    with rig.ctx.db.read() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM phone_file WHERE remote_path LIKE '%/DCIM/Camera/%' AND present = 1"
            )
        ]
    rng = random.Random(seed)
    picked = rng.sample(ids, 7)
    faults = ["edit", "corrupt", "remove_replica", "never_list", "hash_only"]
    rng.shuffle(faults)
    phone = rig.transport.phone
    for pid, fault in zip(picked[:5], faults, strict=True):
        path = path_of(rig, pid)
        if fault == "edit":
            phone.modify(path, b"x", int(time.time()) - 40 * 86400)
        elif fault == "corrupt":
            local_of(rig, pid).write_bytes(b"")
        elif fault == "remove_replica":
            local_of(rig, pid).unlink()
        elif fault == "never_list":
            rig.ctx.config.browse.never_delete_extra.append(path)
        else:
            rig.transport.faults.wrong_sha256.add(path)
    c, h = client(rig)
    run = browse_preflight(rig, c, h, folders=["/sdcard/DCIM/Camera"])
    assert run["state"] == "ready"
    late_edit, late_loss = picked[5], picked[6]
    phone.modify(path_of(rig, late_edit), b"late", int(time.time()) - 40 * 86400)
    local_of(rig, late_loss).unlink()  # a backup lost after the checks
    c.post(
        "/api/delete/execute",
        headers=h,
        json={"run_id": run["id"], "confirm": True, "typed_count": run["passed"]},
    )
    assert rig.ctx.tasks.wait(60)
    gone = set(deletes(rig))
    bad = {path_of(rig, p) for p in picked}
    assert gone and not gone & bad
    for pid in ids:
        if path_of(rig, pid) in gone:
            assert local_of(rig, pid).is_file()


def test_a_folder_means_only_the_files_the_filters_showed(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    c, h = client(rig)
    shown = c.get("/api/browse?path=/sdcard/DCIM/Camera&q=.mp4", headers=h).json()["files"]
    run = browse_preflight(rig, c, h, folders=["/sdcard/DCIM"], filters={"q": ".mp4"})
    assert run["passed"] == len(shown) and all(i["path"].endswith(".mp4") for i in run["items"])
    assert (
        c.post(
            "/api/delete/preflight", headers=h, json={"source": "browse", "folders": [""]}
        ).status_code
        == 422
    )  # never "the whole phone"


def test_browse_may_delete_in_download_but_never_in_app_data(rig: Rig) -> None:
    from sift.cleanup.preflight import in_browse_roots, in_delete_roots

    cfg = rig.ctx.config
    assert in_browse_roots(cfg, "/sdcard/Download/old.apk")
    assert not in_delete_roots(cfg, "/sdcard/Download/old.apk")  # not from the suggestion tabs
    assert not in_browse_roots(cfg, "/sdcard/Android/data/com.app/x")
    assert not in_browse_roots(cfg, "/sdcard/WhatsApp/Databases/msgstore.db.crypt14")


def test_a_loaded_protected_label_counts_before_any_check(rig: Rig) -> None:
    from sift.cleanup.labels_io import load

    backed_up(rig, ["Camera"])
    with rig.ctx.db.read() as conn:
        pid, digest = conn.execute(
            "SELECT id, content_hash FROM phone_file "
            "WHERE remote_path LIKE '%/DCIM/Camera/%' LIMIT 1"
        ).fetchone()
    with rig.ctx.db.tx() as conn:  # loaded while that content was unknown, so it waits
        conn.execute(
            "INSERT INTO label_import (content_hash, label, created_at) VALUES (?, 'protected', 1)",
            (digest,),
        )
    del load
    run_id = create_run(rig.ctx, device_id(rig), "browse", [pid], "archive")
    run_preflight(rig.ctx, run_id, rig.session)
    assert results(rig, run_id)[pid] == "protected"


def test_p9_the_manifest_lists_every_planned_path_whenever_the_app_dies(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    with rig.ctx.db.read() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM phone_file WHERE remote_path LIKE '%/DCIM/Camera/%' AND present = 1"
            )
        ]
    for kill_after in (0, 1, 3):
        run_id = create_run(rig.ctx, device_id(rig), "browse", ids, "archive")
        run_preflight(rig.ctx, run_id, rig.session)
        planned = {path_of(rig, p) for p, r in results(rig, run_id).items() if r == "ok"}
        real = rig.transport.delete
        calls = {"n": 0}

        def dying(path: str, real=real, calls=calls, limit=kill_after) -> None:
            if calls["n"] >= limit:
                raise KeyboardInterrupt  # the app is killed here
            calls["n"] += 1
            real(path)

        rig.transport.delete = dying  # type: ignore[method-assign]
        check_execute(rig.ctx, run_id, rig.session, confirm=True, typed_count=None)
        with pytest.raises(KeyboardInterrupt):
            execute(rig.ctx, run_id, rig.session)
        rig.transport.delete = real  # type: ignore[method-assign]
        with rig.ctx.db.read() as conn:
            manifests = json.loads(
                conn.execute("SELECT manifests FROM delete_run WHERE id = ?", (run_id,)).fetchone()[
                    0
                ]
            )
        listed = {f["path"] for f in json.loads(Path(manifests[0]).read_text())["files"]}
        assert listed == planned


def test_the_archive_browser_lists_the_backup_and_restores_to_the_phone(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    c, h = client(rig)
    dests = c.get("/api/archive/destinations", headers=h).json()
    mac = dests[0]["id"]
    top = c.get(f"/api/archive?dest={mac}", headers=h).json()
    assert top["folders"]
    with rig.ctx.db.read() as conn:
        pid, rel = conn.execute(
            "SELECT p.id, r.rel_path FROM phone_file p JOIN replica r "
            "ON r.content_hash = p.content_hash WHERE p.remote_path LIKE '%/DCIM/Camera/%' LIMIT 1"
        ).fetchone()
    path = path_of(rig, pid)
    entry = rig.transport.phone.entry(path)
    assert entry is not None
    rig.transport.phone.remove(path)  # gone from the phone (deleted earlier)
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE phone_file SET present = 0, state = 'deleted' WHERE id = ?", (pid,))

    def restore() -> dict:
        assert (
            c.post(
                "/api/archive/restore", headers=h, json={"dest": mac, "paths": [rel, rel]}
            ).status_code
            == 200
        )
        assert rig.ctx.tasks.wait(60)
        return rig.ctx.tasks.current().result

    assert restore() == {"restored": 1, "already_on_phone": 0, "failed": 0, "not_done": 0}
    back = rig.transport.phone.entry(path)
    assert back is not None and back.mtime == entry.mtime
    assert restore()["already_on_phone"] == 1
