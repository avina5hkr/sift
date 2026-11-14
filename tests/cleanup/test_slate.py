"""M12: Clean slate. P7 (I11), the plan and its gaps, the run, a 50k-file plan."""

import time

from hypothesis import given, settings
from hypothesis import strategies as st

from sift.cleanup import slate
from sift.cleanup.browse import FileRow
from sift.cleanup.lexicons import load
from sift.cleanup.quarantine import entries
from sift.core.config import Config, phone_path_key
from tests.backup.test_confirm import backed_up, device_id
from tests.backup.test_run import Rig
from tests.cleanup.test_browse import client
from tests.cleanup.test_delete import deletes

NOW = 1_800_000_000
FOLDERS = [
    "/sdcard/DCIM/Camera",
    "/sdcard/Documents",
    "/sdcard/Download",
    "/sdcard/Pictures",
    "/sdcard/Android/data/app",
    "/sdcard/Movies/Trips",
    "/sdcard/DCIM/Screenshots",
]

files_st = st.lists(
    st.fixed_dictionaries(
        {
            "folder": st.sampled_from(FOLDERS),
            "name": st.sampled_from(["a.jpg", "b.pdf", "c.mp4", "note.txt", "IMG.jpg"]),
            "age_days": st.integers(0, 400),
            "origin": st.sampled_from(["camera", "screenshot", "other", "whatsapp_received"]),
            "label": st.sampled_from([None, "keep", "junk", "protected"]),
            "text": st.sampled_from([None, "good morning", "invoice 42", "hello"]),
        }
    ),
    min_size=1,
    max_size=30,
)
rules_st = st.builds(
    slate.Rules,
    keep_folders=st.lists(st.sampled_from(FOLDERS), max_size=3),
    keep_newer_than_days=st.sampled_from([0, 7, 30, 365]),
    keep_labelled=st.booleans(),
    keep_camera=st.booleans(),
    patterns=st.lists(st.sampled_from(["/sdcard/Download/*.pdf", "/sdcard/*/*.txt"]), max_size=2),
)


@settings(max_examples=300, deadline=None)
@given(
    files_st,
    rules_st,
    st.lists(st.integers(0, 29), max_size=5),
    st.lists(st.sampled_from(FOLDERS), max_size=2),
)
def test_p7_no_kept_or_protected_file_is_ever_in_a_removal_set(
    files: list[dict], rules: slate.Rules, kept: list[int], kept_folders: list[str]
) -> None:
    cfg = Config()
    doc = load("document")
    for n, f in enumerate(files):
        path = f"{f['folder']}/{n}-{f['name']}"
        row = FileRow(
            n, path, 10, NOW - f["age_days"] * 86400, f["origin"], "image", "mac_only", "h"
        )
        info = slate.Info(f["label"], f["text"], f["origin"] == "screenshot")
        d = slate.decide(
            row, info, rules, cfg, doc, kept_ids=set(kept), kept_folders=kept_folders, now=NOW
        )
        key = phone_path_key(path)
        must_stay = (
            key.startswith("/sdcard/android/data")
            or f["label"] == "protected"
            or f["text"] == "invoice 42"
            or f["origin"] == "screenshot"
            or "/screenshots/" in key
            or n in kept
            or any(
                key.startswith(phone_path_key(k) + "/")
                for k in [*kept_folders, *rules.keep_folders]
            )
            or (rules.keep_newer_than_days and f["age_days"] < rules.keep_newer_than_days)
            or (rules.keep_labelled and f["label"] == "keep")
            or (rules.keep_camera and f["origin"] == "camera")
            or any(__import__("fnmatch").fnmatch(key, phone_path_key(p)) for p in rules.patterns)
        )
        if must_stay:
            assert d.action != "remove", (path, f, rules, d)


def everything(rig: Rig) -> None:
    names = [s.name for s in rig.ctx.config.backup.sets if s.enabled]
    backed_up(rig, names)
    with rig.ctx.db.tx() as conn:  # as after an Analyze: indexed and read
        conn.execute(
            "UPDATE content SET analysis_version = 2, ocr_version = 1, ocr_lang = 'hi-IN+en-US'"
        )


def test_clean_slate_plans_waits_for_gaps_and_removes_only_the_plan(rig: Rig) -> None:
    everything(rig)
    with rig.ctx.db.tx() as conn:  # a file Sift saw but no backup set covers
        conn.execute(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, present) "
            "VALUES (1, '/sdcard/Download/stray.bin', 'other', 5, 1500000000, 'discovered', 1)"
        )
    c, h = client(rig)
    plan = c.post(
        "/api/slate/plan", headers=h, json={"keep_folders": [], "keep_newer_than_days": 0}
    ).json()
    assert plan["remove"]["files"] > 0 and plan["protected"]["files"] > 0  # screenshots stay
    assert plan["gaps"]["files"] >= 1 and plan["gaps_no_set"]["files"] >= 1
    if plan["gaps"]["files"]:
        refused = c.post(f"/api/slate/plan/{plan['id']}/preflight", headers=h, json={})
        assert refused.status_code == 409 and refused.json()["code"] == "gaps"
        gaps = c.get(f"/api/slate/plan/{plan['id']}/files?side=gaps", headers=h).json()["files"]
        c.post(
            f"/api/slate/plan/{plan['id']}/keep", headers=h, json={"ids": [g["id"] for g in gaps]}
        )
        plan = c.get(f"/api/slate/plan/{plan['id']}", headers=h).json()
    assert plan["gaps"]["files"] == 0
    remove = c.get(f"/api/slate/plan/{plan['id']}/files?side=remove", headers=h).json()
    kept_paths = {
        f["path"]
        for side in ("keep", "protected")
        for f in c.get(f"/api/slate/plan/{plan['id']}/files?side={side}", headers=h).json()["files"]
    }
    started = c.post(
        f"/api/slate/plan/{plan['id']}/preflight",
        headers=h,
        json={"dispositions": {"camera": "quarantine"}},
    ).json()
    assert rig.ctx.tasks.wait(60)
    run = c.get(f"/api/delete/runs/{started['run_id']}", headers=h).json()
    assert run["typed_required"] is True and run["passed"] > 0
    assert (
        c.post(
            "/api/delete/execute", headers=h, json={"run_id": run["id"], "confirm": True}
        ).status_code
        == 409
    )
    c.post(
        "/api/delete/execute", headers=h,
        json={"run_id": run["id"], "confirm": True, "typed_count": run["passed"]},
    )  # fmt: skip
    assert rig.ctx.tasks.wait(60) and rig.ctx.tasks.current().state == "done", (
        rig.ctx.tasks.current()
    )
    gone = set(deletes(rig))
    assert gone and gone <= {f["path"] for f in remove["files"]}
    assert not gone & kept_paths  # I11
    camera_gone = [p for p in gone if "/DCIM/Camera/" in p]
    assert len(entries(rig.ctx)) >= len(camera_gone) > 0  # camera chose quarantine


def ready_plan(rig: Rig, c, h, **rules) -> dict:
    plan = c.post(
        "/api/slate/plan", headers=h, json={"keep_folders": [], "keep_newer_than_days": 0, **rules}
    ).json()
    gaps = c.get(f"/api/slate/plan/{plan['id']}/files?side=gaps", headers=h).json()["files"]
    if gaps:
        plan = c.post(
            f"/api/slate/plan/{plan['id']}/keep", headers=h, json={"ids": [g["id"] for g in gaps]}
        ).json()
    return plan


def test_a_file_kept_after_the_checks_is_never_removed(rig: Rig) -> None:
    everything(rig)
    c, h = client(rig)
    plan = ready_plan(rig, c, h)
    started = c.post(
        f"/api/slate/plan/{plan['id']}/preflight",
        headers=h,
        json={"fingerprint": plan["fingerprint"]},
    ).json()
    assert rig.ctx.tasks.wait(60)
    first = c.get(f"/api/slate/plan/{plan['id']}/files?side=remove", headers=h).json()["files"][0]
    kept = c.post(f"/api/slate/plan/{plan['id']}/keep", headers=h, json={"ids": [first["id"]]})
    assert kept.status_code == 200
    run = c.get(f"/api/delete/runs/{started['run_id']}", headers=h).json()
    assert run["state"] == "blocked"  # the checks no longer match the plan
    refused = c.post(
        "/api/delete/execute", headers=h,
        json={"run_id": run["id"], "confirm": True, "typed_count": run["passed"]},
    )  # fmt: skip
    assert refused.status_code == 409 and deletes(rig) == []
    # even a run left "ready" is refused when the plan now keeps one of its files
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE delete_run SET state = 'ready' WHERE id = ?", (run["id"],))
    refused = c.post(
        "/api/delete/execute", headers=h,
        json={"run_id": run["id"], "confirm": True, "typed_count": run["passed"]},
    )  # fmt: skip
    assert refused.status_code == 409 and refused.json()["code"] == "plan_changed"
    stale = c.post(
        f"/api/slate/plan/{plan['id']}/preflight", headers=h, json={"fingerprint": "old"}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "plan_changed"


def test_pictures_not_analysed_yet_are_protected(rig: Rig) -> None:
    names = [s.name for s in rig.ctx.config.backup.sets if s.enabled]
    backed_up(rig, names)  # no Analyze yet
    c, h = client(rig)
    plan = ready_plan(rig, c, h)
    files = c.get(f"/api/slate/plan/{plan['id']}/files?side=remove", headers=h).json()["files"]
    assert not [f for f in files if f["kind"] == "image"]
    prot = c.get(f"/api/slate/plan/{plan['id']}/files?side=protected", headers=h).json()["files"]
    assert any(f["reason"] == "Not analysed yet" for f in prot)


def test_keep_paths_are_normalised_or_refused_and_plain_paths_keep_below(rig: Rig) -> None:
    everything(rig)
    c, h = client(rig)
    bad = c.post("/api/slate/plan", headers=h, json={"keep_folders": ["C:/Photos"]})
    assert bad.status_code == 422 and bad.json()["code"] == "bad_keep_path"
    plan = c.post(
        "/api/slate/plan",
        headers=h,
        json={"keep_folders": ["/storage/emulated/0/DCIM"], "keep_newer_than_days": 0,
              "patterns": ["/sdcard/Pictures", "*.mp4"]},
    ).json()  # fmt: skip
    assert plan["rules"]["keep_folders"] == ["/sdcard/DCIM"]
    assert plan["rules"]["patterns"] == ["/sdcard/Pictures", "/sdcard/*.mp4"]
    remove = c.get(f"/api/slate/plan/{plan['id']}/files?side=remove", headers=h).json()["files"]
    assert not [f for f in remove if f["path"].startswith(("/sdcard/DCIM/", "/sdcard/Pictures/"))]
    assert not [f for f in remove if f["path"].endswith(".mp4")]
    assert slate.pattern_keeps("/sdcard/a/b/x.pdf", "/sdcard/**/x.pdf")
    assert slate.pattern_keeps("/sdcard/a/x.pdf", "/sdcard/a/**/x.pdf")


def test_archive_categories_are_never_quarantined(rig: Rig) -> None:
    everything(rig)
    c, h = client(rig)
    with rig.ctx.db.tx() as conn:  # junk-labelled camera pictures: archive was chosen for camera
        conn.execute(
            "INSERT OR IGNORE INTO label (content_hash, label, source, created_at) "
            "SELECT content_hash, 'junk', 'manual', 1 FROM phone_file "
            "WHERE remote_path LIKE '%/DCIM/Camera/%' AND content_hash IS NOT NULL"
        )
    plan = ready_plan(rig, c, h)
    started = c.post(
        f"/api/slate/plan/{plan['id']}/preflight", headers=h,
        json={"dispositions": {c_: "archive" for c_ in slate.CATEGORIES}},
    ).json()  # fmt: skip
    assert rig.ctx.tasks.wait(60)
    run = c.get(f"/api/delete/runs/{started['run_id']}", headers=h).json()
    c.post(
        "/api/delete/execute", headers=h,
        json={"run_id": run["id"], "confirm": True, "typed_count": run["passed"]},
    )  # fmt: skip
    assert rig.ctx.tasks.wait(60) and deletes(rig)
    assert entries(rig.ctx) == []  # archive everywhere: nothing went to quarantine


def test_p8_faults_through_the_slate_path(rig: Rig) -> None:
    import random

    from tests.cleanup.test_delete import local_of, path_of

    everything(rig)
    c, h = client(rig)
    plan = ready_plan(rig, c, h)
    remove = c.get(f"/api/slate/plan/{plan['id']}/files?side=remove", headers=h).json()["files"]
    backed = [f["id"] for f in remove if f["path"].startswith("/sdcard/DCIM/Camera/")]
    rng = random.Random(5)
    edited, corrupted, hashed, late = rng.sample(backed, 4)
    phone = rig.transport.phone
    phone.modify(path_of(rig, edited), b"x", int(time.time()) - 40 * 86400)
    local_of(rig, corrupted).write_bytes(b"")
    rig.transport.faults.wrong_sha256.add(path_of(rig, hashed))
    started = c.post(f"/api/slate/plan/{plan['id']}/preflight", headers=h, json={}).json()
    assert rig.ctx.tasks.wait(60)
    phone.modify(path_of(rig, late), b"late", int(time.time()) - 40 * 86400)
    run = c.get(f"/api/delete/runs/{started['run_id']}", headers=h).json()
    c.post(
        "/api/delete/execute", headers=h,
        json={"run_id": run["id"], "confirm": True, "typed_count": run["passed"]},
    )  # fmt: skip
    assert rig.ctx.tasks.wait(60)
    gone = set(deletes(rig))
    assert gone and not gone & {path_of(rig, p) for p in (edited, corrupted, hashed, late)}


def test_a_50k_file_plan_takes_seconds(rig: Rig) -> None:
    with rig.ctx.db.tx() as conn:
        conn.executemany(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, present) "
            "VALUES (1, ?, 'other', 1000, 1600000000, 'discovered', 1)",
            [(f"/sdcard/Pictures/bulk/{i // 1000}/f{i}.jpg",) for i in range(50_000)],
        )
    plan_id = slate.create(rig.ctx, device_id(rig), slate.Rules([], 0))
    started = time.perf_counter()
    items = slate.plan(rig.ctx, plan_id)
    took = time.perf_counter() - started
    assert len(items) >= 50_000 and took < 15, took
    assert slate.summary(items)["gaps"]["files"] >= 50_000  # none of them is backed up
