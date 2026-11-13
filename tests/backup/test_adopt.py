"""Adoption of an existing backup folder: indexed in place, nothing in it ever changes."""

import hashlib
import logging
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sift.backup.adopt import adopt, provisional_count, record
from sift.core.clock import SystemClock
from sift.core.context import AppContext
from tests.backup.test_replicator import add_dest, replicas
from tests.helpers import make_ctx

NAMES = st.text(alphabet="abcXYZ é_-.0", min_size=1, max_size=8).filter(
    lambda n: n not in {".", "..", ".staging", ".sift", ".DS_Store"} and not n.startswith("._")
)
TREES = st.dictionaries(
    st.lists(NAMES, min_size=1, max_size=3).map(tuple),
    st.binary(min_size=0, max_size=64),
    min_size=1,
    max_size=25,
)


def snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st_ = p.lstat()
            out[str(p.relative_to(root))] = (p.read_bytes(), st_.st_mtime_ns, st_.st_ino)
    return out


def build(root: Path, tree: dict[tuple[str, ...], bytes]) -> None:
    for parts, data in tree.items():
        path = root.joinpath(*parts)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
        except FileExistsError, NotADirectoryError, IsADirectoryError:
            continue  # a name used both as a folder and a file: keep the first


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(tree=TREES)
def test_adoption_never_modifies_existing_files(paths, tmp_path_factory, tree) -> None:
    ctx = make_ctx(paths)
    root = tmp_path_factory.mktemp("adopt")
    build(root, tree)
    before = snapshot(root)
    dest = add_dest(ctx, "mac", root)
    stats = adopt(ctx.db, dest)
    assert snapshot(root) == before  # bytes, mtime and inode of every file; no file added
    assert stats.found == len(before)
    contents = {hashlib.sha256(v[0]).hexdigest() for v in before.values()}
    assert stats.indexed == len(contents) and stats.duplicates == len(before) - len(contents)
    assert set(replicas(ctx, dest)) <= set(before)
    ctx.db.close()


def test_provisional_files_do_not_count_until_hashed(ctx: AppContext, tmp_path: Path) -> None:
    root = tmp_path / "old"
    (root / "DCIM").mkdir(parents=True)
    (root / "DCIM/a.jpg").write_bytes(b"a")
    (root / "DCIM/b.jpg").write_bytes(b"b")
    dest = add_dest(ctx, "mac", root)
    record(ctx.db, dest, SystemClock())
    assert provisional_count(ctx.db) == 2 and replicas(ctx, dest) == set()
    stops = iter([False, True])
    adopt(ctx.db, dest, should_stop=lambda: next(stops, True))
    assert provisional_count(ctx.db) == 1 and len(replicas(ctx, dest)) == 1
    adopt(ctx.db, dest)  # resumes
    assert provisional_count(ctx.db) == 0 and replicas(ctx, dest) == {"DCIM/a.jpg", "DCIM/b.jpg"}


def test_fast_adopt_mode_warns_and_hashes(
    ctx: AppContext, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "old"
    root.mkdir()
    (root / "x.jpg").write_bytes(b"x")
    dest = add_dest(ctx, "mac", root)
    with caplog.at_level(logging.WARNING):
        stats = adopt(ctx.db, dest, mode="fast")
    assert "not built" in caplog.text and stats.indexed == 1


def test_links_fifos_and_sift_folders_are_never_opened(ctx: AppContext, tmp_path: Path) -> None:
    root = tmp_path / "old"
    (root / ".staging").mkdir(parents=True)
    (root / ".staging/x.part").write_bytes(b"part")
    (root / "real.jpg").write_bytes(b"real")
    os.symlink("/etc/hosts", root / "link.jpg")
    os.mkfifo(root / "pipe.jpg")
    dest = add_dest(ctx, "mac", root)
    stats = adopt(ctx.db, dest)
    assert stats.found == 1 and replicas(ctx, dest) == {"real.jpg"}


def test_files_sift_already_backed_up_are_not_recorded_again(
    ctx: AppContext, tmp_path: Path
) -> None:
    from tests.backup.test_replicator import put

    dest = add_dest(ctx, "mac", tmp_path / "mac")
    put(ctx, dest, "Phone/a.jpg", b"a")
    (dest.root / "Phone/extra.jpg").write_bytes(b"a")  # the same bytes under another name
    stats = adopt(ctx.db, dest)
    assert (stats.found, stats.new, stats.duplicates) == (2, 1, 1)
    assert replicas(ctx, dest) == {"Phone/a.jpg"}


def test_adopt_over_the_api(ctx: AppContext, tmp_path: Path) -> None:

    from sift.services.plainwords import describe
    from sift.services.tasks import TaskRunner
    from tests.helpers import H, client_for

    root = tmp_path / "mac"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"a")
    add_dest(ctx, "mac", root)
    ctx.config.backup.mac_root = str(root)
    ctx.tasks = TaskRunner(ctx, describe)
    client = client_for(ctx)
    assert (
        client.post("/api/backup/adopt", headers=H, json={"destination": "ssd"}).status_code == 409
    )
    assert (
        client.post("/api/backup/adopt", headers=H, json={"destination": "mac"}).status_code == 200
    )
    assert ctx.tasks.wait(10)
    assert ctx.tasks.current().result["indexed"] == 1


@pytest.mark.slow
def test_adopting_20k_files_changes_none_of_them(ctx: AppContext, tmp_path: Path) -> None:
    root = tmp_path / "big"
    for d in range(100):
        folder = root / f"DCIM/{d:03d}"
        folder.mkdir(parents=True)
        for f in range(200):
            (folder / f"IMG_{f:04d}.jpg").write_bytes(f"{d}-{f}".encode())
    before = snapshot(root)
    stats = adopt(ctx.db, add_dest(ctx, "mac", root))
    assert stats.found == stats.indexed == 20_000
    assert snapshot(root) == before
