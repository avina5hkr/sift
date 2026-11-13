"""Verify, Problems and Repair on temp destinations."""

import time
from pathlib import Path

from sift.backup.problems import open_problems
from sift.backup.replicator import Dest, Replicator
from sift.backup.verifier import repair, verify_dest
from sift.core.context import AppContext
from sift.services.plainwords import describe
from sift.services.tasks import TaskRunner
from tests.backup.test_replicator import add_dest, put, replicas, tree
from tests.helpers import H, client_for


def setup_pair(ctx: AppContext, tmp_path: Path) -> tuple[Dest, Dest]:
    mac, ssd = add_dest(ctx, "mac", tmp_path / "mac"), add_dest(ctx, "ssd", tmp_path / "ssd")
    put(ctx, mac, "Phone/a.jpg", b"aaaa")
    put(ctx, mac, "Phone/b.jpg", b"bbbb")
    Replicator(ctx.db).replicate(mac, ssd)
    return mac, ssd


def test_verify_finds_damage_and_missing_files_and_touches_nothing(
    ctx: AppContext, tmp_path: Path
) -> None:
    _mac, ssd = setup_pair(ctx, tmp_path)
    (ssd.root / "Phone/a.jpg").write_bytes(b"AAAA")
    (ssd.root / "Phone/b.jpg").unlink()
    stats = verify_dest(ctx.db, ssd)
    assert (stats.checked, stats.ok, stats.mismatched, stats.missing) == (2, 0, 1, 1)
    assert replicas(ctx, ssd) == set()
    assert (ssd.root / "Phone/a.jpg").read_bytes() == b"AAAA"  # left alone (I1)
    kinds = sorted((p.kind, p.rel_path, p.repairable) for p in open_problems(ctx.db))
    assert kinds == [("hash_mismatch", "Phone/a.jpg", True), ("missing_file", "Phone/b.jpg", True)]


def test_verify_skips_an_unplugged_drive(ctx: AppContext, tmp_path: Path) -> None:
    _mac, ssd = setup_pair(ctx, tmp_path)
    gone = Dest(ssd.id, "ssd", tmp_path / "not-mounted")
    assert verify_dest(ctx.db, gone).skipped
    assert replicas(ctx, ssd) == {"Phone/a.jpg", "Phone/b.jpg"}


def test_repair_puts_a_good_copy_beside_the_damaged_one(ctx: AppContext, tmp_path: Path) -> None:
    _mac, ssd = setup_pair(ctx, tmp_path)
    (ssd.root / "Phone/a.jpg").write_bytes(b"AAAA")
    (ssd.root / "Phone/b.jpg").unlink()
    verify_dest(ctx.db, ssd)
    for problem in open_problems(ctx.db):
        repair(ctx.db, problem.id)
    assert open_problems(ctx.db) == []
    files = tree(ssd)
    assert files["Phone/a.jpg"] == b"AAAA"  # the damaged file is still there
    assert files["Phone/b.jpg"] == b"bbbb"  # the missing one is back under its name
    assert sorted(v for k, v in files.items() if k.startswith("Phone/a_")) == [b"aaaa"]
    assert verify_dest(ctx.db, ssd).ok == 2


def test_verify_and_problems_over_the_api(ctx: AppContext, tmp_path: Path) -> None:
    mac, ssd = setup_pair(ctx, tmp_path)
    (ssd.root / "Phone/a.jpg").write_bytes(b"AAAA")
    ctx.config.backup.mac_root = str(mac.root)
    ctx.config.backup.ssd_volume_uuid = "uuid-ssd"

    class Plugged:  # the SSD watcher, with this SSD plugged in
        def state(self):
            from sift.services.ssd import SsdState

            return SsdState(True, True, "SSD", str(ssd.root.parent), str(ssd.root), None, ssd.id)

    ctx.ssd = Plugged()  # type: ignore[assignment]
    ctx.tasks = TaskRunner(ctx, describe)
    client = client_for(ctx)
    assert client.post("/api/backup/verify", headers=H).status_code == 200
    deadline = time.monotonic() + 10
    while ctx.tasks.current() is None or ctx.tasks.current().state == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert ctx.tasks.current().result["mismatched"] == 1
    problems = client.get("/api/backup/problems", headers=H).json()
    assert [p["rel_path"] for p in problems] == ["Phone/a.jpg"]
    fixed = client.post(f"/api/backup/problems/{problems[0]['id']}/repair", headers=H)
    assert fixed.status_code == 200 and fixed.json()["rel_path"].startswith("Phone/a_")
    assert client.get("/api/backup/problems", headers=H).json() == []
    assert client.post("/api/backup/problems/999/repair", headers=H).status_code == 404
    assert client.post("/api/backup/verify").status_code == 401  # needs the token


def test_unplugging_during_verify_drops_nothing(ctx: AppContext, tmp_path: Path) -> None:
    import shutil

    _mac, ssd = setup_pair(ctx, tmp_path)
    for i in range(4):
        put(ctx, ssd, f"Phone/x{i}.jpg", bytes([i]) * 9)
    before = replicas(ctx, ssd)

    def unplug(done: int, _total: int) -> None:
        if done == 2 and ssd.root.exists():
            shutil.move(ssd.root, tmp_path / "away")

    stats = verify_dest(ctx.db, ssd, on_file=unplug)
    assert stats.skipped and stats.missing == 0
    assert replicas(ctx, ssd) == before and open_problems(ctx.db) == []
