"""Replication between two destinations (Mac and SSD) on temp folders."""

import hashlib
import os
from pathlib import Path

import pytest

from sift.backup.layout import rel_key
from sift.backup.replicator import Dest, Replicator, self_heal
from sift.core.context import AppContext


def add_dest(ctx: AppContext, kind: str, root: Path) -> Dest:
    with ctx.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO destination (kind, root_path) VALUES (?, ?)", (kind, str(root))
        )
    root.mkdir(parents=True, exist_ok=True)
    return Dest(int(cur.lastrowid or 0), kind, root)


def put(ctx: AppContext, dest: Dest, rel: str, data: bytes, *, row: bool = True) -> str:
    path = dest.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if row:
        with ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO content (hash, size, kind, created_at) VALUES (?, ?, 'image', 1) "
                "ON CONFLICT (hash) DO NOTHING",
                (digest, len(data)),
            )
            conn.execute(
                "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (digest, dest.id, rel, rel_key(rel), len(data)),
            )
    return digest


def tree(dest: Dest) -> dict[str, bytes]:
    return {
        str(p.relative_to(dest.root)): p.read_bytes()
        for p in dest.root.rglob("*")
        if p.is_file() and ".staging" not in p.parts
    }


def replicas(ctx: AppContext, dest: Dest) -> set[str]:
    with ctx.db.read() as conn:
        return {
            r[0] for r in conn.execute("SELECT rel_path FROM replica WHERE dest_id = ?", (dest.id,))
        }


@pytest.fixture
def two(ctx: AppContext, tmp_path: Path) -> tuple[Dest, Dest]:
    return add_dest(ctx, "mac", tmp_path / "mac"), add_dest(ctx, "ssd", tmp_path / "ssd" / "B")


def test_bidirectional_replication_yields_union(ctx: AppContext, two: tuple[Dest, Dest]) -> None:
    mac, ssd = two
    put(ctx, mac, "Phone/DCIM/a.jpg", b"a" * 10)
    put(ctx, ssd, "Phone/DCIM/b.jpg", b"b" * 20)
    put(ctx, mac, "Phone/both.jpg", b"c")
    put(ctx, ssd, "Phone/both.jpg", b"c")
    one, other = Replicator(ctx.db).replicate_union(mac, ssd)
    assert (one.copied, other.copied) == (1, 1)
    assert tree(mac) == tree(ssd)
    assert (
        replicas(ctx, mac)
        == replicas(ctx, ssd)
        == {"Phone/DCIM/a.jpg", "Phone/DCIM/b.jpg", "Phone/both.jpg"}
    )
    assert not list((ssd.root / ".staging").glob("*.part"))
    again = Replicator(ctx.db).replicate_union(mac, ssd)
    assert again[0].copied == again[1].copied == 0


def test_a_corrupt_source_is_never_copied_and_is_listed(
    ctx: AppContext, two: tuple[Dest, Dest]
) -> None:
    mac, ssd = two
    put(ctx, mac, "Phone/x.jpg", b"good bytes")
    (mac.root / "Phone/x.jpg").write_bytes(b"bad! bytes")  # same size, different bytes
    stats = Replicator(ctx.db).replicate(mac, ssd)
    assert stats.corrupt == 1 and tree(ssd) == {}
    assert replicas(ctx, mac) == set()  # no longer counted as verified
    assert (mac.root / "Phone/x.jpg").read_bytes() == b"bad! bytes"  # left alone (I1)
    with ctx.db.read() as conn:
        assert conn.execute("SELECT kind, rel_path FROM problem").fetchall() == [
            ("hash_mismatch", "Phone/x.jpg")
        ]


def test_existing_files_are_never_overwritten(ctx: AppContext, two: tuple[Dest, Dest]) -> None:
    mac, ssd = two
    put(ctx, mac, "Phone/same.jpg", b"same")
    put(ctx, ssd, "Phone/same.jpg", b"same", row=False)  # e.g. a crash before the row
    put(ctx, mac, "Phone/clash.jpg", b"new content")
    put(ctx, ssd, "Phone/clash.jpg", b"other bytes", row=False)
    before = (ssd.root / "Phone/clash.jpg").stat()
    stats = Replicator(ctx.db).replicate(mac, ssd)
    assert stats.reused == 1 and stats.copied == 1
    assert (ssd.root / "Phone/clash.jpg").read_bytes() == b"other bytes"
    assert (ssd.root / "Phone/clash.jpg").stat().st_ino == before.st_ino
    clash = [p for p in replicas(ctx, ssd) if p.startswith("Phone/clash_")]
    assert len(clash) == 1 and (ssd.root / clash[0]).read_bytes() == b"new content"


def test_self_heal_drops_rows_of_missing_files_but_not_of_an_unmounted_drive(
    ctx: AppContext, two: tuple[Dest, Dest], tmp_path: Path
) -> None:
    _mac, ssd = two
    put(ctx, ssd, "Phone/a.jpg", b"a")
    put(ctx, ssd, "Phone/b.jpg", b"b")
    (ssd.root / "Phone/a.jpg").unlink()
    assert self_heal(ctx.db, ssd) == 1 and replicas(ctx, ssd) == {"Phone/b.jpg"}
    unplugged = Dest(ssd.id, "ssd", tmp_path / "gone" / "B")
    assert self_heal(ctx.db, unplugged) == 0 and replicas(ctx, ssd) == {"Phone/b.jpg"}


def test_ssd_unplugged_mid_replication_leaves_no_false_rows(
    ctx: AppContext, two: tuple[Dest, Dest], monkeypatch: pytest.MonkeyPatch
) -> None:
    mac, ssd = two
    for i in range(5):
        put(ctx, mac, f"Phone/{i}.jpg", bytes([i]) * 100)
    real = Replicator.copy_one
    count = {"n": 0}

    def flaky(self: Replicator, *args: object, **kwargs: object) -> tuple[str, bool]:
        count["n"] += 1
        if count["n"] >= 3:
            raise OSError(6, "Device not configured")  # the drive went away
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Replicator, "copy_one", flaky)
    stats = Replicator(ctx.db).replicate(mac, ssd)
    assert stats.copied == 2 and stats.failed == 3
    assert replicas(ctx, ssd) == set(tree(ssd))  # every row has its file, nothing extra
    monkeypatch.setattr(Replicator, "copy_one", real)
    assert Replicator(ctx.db).replicate(mac, ssd).copied == 3
    assert tree(mac) == tree(ssd)


def test_stop_is_honoured_between_files(ctx: AppContext, two: tuple[Dest, Dest]) -> None:
    mac, ssd = two
    for i in range(4):
        put(ctx, mac, f"Phone/{i}.jpg", bytes([i]) * 10)
    done: list[int] = []
    stats = Replicator(ctx.db).replicate(
        mac, ssd, should_stop=lambda: len(done) >= 2, on_file=lambda n, _b: done.append(n)
    )
    assert stats.copied == 2


def test_mtime_is_kept(ctx: AppContext, two: tuple[Dest, Dest]) -> None:
    mac, ssd = two
    put(ctx, mac, "Phone/old.jpg", b"old")
    os.utime(mac.root / "Phone/old.jpg", (1_600_000_000, 1_600_000_000))
    Replicator(ctx.db).replicate(mac, ssd)
    assert int((ssd.root / "Phone/old.jpg").stat().st_mtime) == 1_600_000_000


def test_unplugging_the_source_drive_mid_copy_loses_no_rows(
    ctx: AppContext, two: tuple[Dest, Dest], monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    mac, ssd = two
    for i in range(5):
        put(ctx, ssd, f"Phone/{i}.jpg", bytes([i]) * 50)
    hidden = ssd.root.parent / "unplugged"
    real = Replicator.copy_one
    count = {"n": 0}

    def unplug_after_two(self: Replicator, *args: object, **kwargs: object) -> tuple[str, bool]:
        count["n"] += 1
        if count["n"] == 3:
            shutil.move(ssd.root, hidden)  # the drive goes away
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Replicator, "copy_one", unplug_after_two)
    stats = Replicator(ctx.db).replicate(ssd, mac)
    assert stats.copied == 2 and stats.healed == 0
    assert len(replicas(ctx, ssd)) == 5  # an unplugged drive's files are not "lost"
