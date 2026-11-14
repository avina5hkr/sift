"""M12: Clean slate. P7 (I11), the plan and its gaps, the run, a 50k-file plan."""

import time

from hypothesis import given, settings
from hypothesis import strategies as st

from sift.cleanup import slate
from sift.cleanup.browse import FileRow
from sift.cleanup.lexicons import load
from sift.cleanup.preflight import run_preflight
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


def test_clean_slate_plans_waits_for_gaps_and_removes_only_the_plan(rig: Rig) -> None:
    everything(rig)
    rig.transport.phone.add_file("/sdcard/Download/stray.bin", b"stray", mtime=1_500_000_000)
    from tests.backup.test_run import finished

    jobs = rig.jobs()
    finished(jobs, jobs.start_backup(["Camera"], dry_run=True))  # the stray file is seen
    c, h = client(rig)
    plan = c.post(
        "/api/slate/plan", headers=h, json={"keep_folders": [], "keep_newer_than_days": 0}
    ).json()
    assert plan["remove"]["files"] > 0 and plan["protected"]["files"] > 0  # screenshots stay
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


def test_a_file_kept_after_the_plan_is_never_removed(rig: Rig) -> None:
    everything(rig)
    c, h = client(rig)
    plan = c.post(
        "/api/slate/plan", headers=h, json={"keep_folders": [], "keep_newer_than_days": 0}
    ).json()
    gaps = c.get(f"/api/slate/plan/{plan['id']}/files?side=gaps", headers=h).json()["files"]
    if gaps:
        c.post(
            f"/api/slate/plan/{plan['id']}/keep", headers=h, json={"ids": [g["id"] for g in gaps]}
        )
    started = c.post(f"/api/slate/plan/{plan['id']}/preflight", headers=h, json={}).json()
    assert rig.ctx.tasks.wait(60)
    first = c.get(f"/api/slate/plan/{plan['id']}/files?side=remove", headers=h).json()["files"][0]
    slate.keep(rig.ctx, plan["id"], [first["id"]], [])  # the owner changes their mind
    run_preflight(rig.ctx, started["run_id"], rig.session)  # the checks run again
    run = c.get(f"/api/delete/runs/{started['run_id']}", headers=h).json()
    assert next(i for i in run["items"] if i["id"] == first["id"])["check"] == "kept"


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
