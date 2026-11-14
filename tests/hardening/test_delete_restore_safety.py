"""Hardening (external review, priority 1): deletion and restore never trust a damaged or changed
copy. Each test reproduces a reviewed defect on the fake phone with temporary backup folders."""

import os

import pytest

from sift.backup.confirm import current_dest_ids
from sift.cleanup.archive import restore as archive_restore
from sift.cleanup.quarantine import entries, reconcile
from sift.cleanup.quarantine import restore as q_restore
from tests.backup.test_run import Rig
from tests.cleanup.test_delete import (
    camera,
    deletes,
    local_of,
    path_of,
    preflight,
    results,
    run,
    whatsapp,
)


def damage_same_size(path) -> None:
    """Flip bytes in a backup copy, keeping its size and mtime (bit rot, a bad edit)."""
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[0] ^= 0xFF
    data[-1] ^= 0xFF
    path.chmod(0o644)
    path.write_bytes(bytes(data))
    os.utime(path, (st.st_atime, st.st_mtime))


# -- 1. same-size backup damage never authorises a delete ------------------------------------


def test_a_damaged_backup_fails_the_checks(rig: Rig) -> None:
    ids = camera(rig)[:3]
    damage_same_size(local_of(rig, ids[0]))
    run_id, counts = preflight(rig, ids)
    assert results(rig, run_id)[ids[0]] == "backup_corrupt"
    assert counts.get("ok") == 2


def test_backup_damage_after_the_checks_stops_that_delete(rig: Rig) -> None:
    ids = camera(rig)[:3]
    run_id, counts = preflight(rig, ids)
    assert counts == {"ok": 3}
    damage_same_size(local_of(rig, ids[0]))
    done = run(rig, run_id)
    assert path_of(rig, ids[0]) not in deletes(rig)
    assert rig.transport.phone.entry(path_of(rig, ids[0])) is not None
    assert done["deleted"] == 2 and done["failed"] == 1


def with_ssd_copies(rig: Rig, tmp_path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """A required SSD (plugged in) holding identical copies of every Mac replica."""
    from pathlib import Path

    import sift.cleanup.verify as verify
    from sift.backup.replicator import Dest

    mac = current_dest_ids(rig.ctx.db, rig.ctx.config)["mac"]
    ssd_root = tmp_path / "ssd"
    with rig.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO destination (kind, root_path, volume_uuid) VALUES ('ssd', ?, 'U-1')",
            (str(ssd_root),),
        )
        ssd = conn.execute("SELECT id FROM destination WHERE kind = 'ssd'").fetchone()[0]
        mac_root = Path(conn.execute(
            "SELECT root_path FROM destination WHERE id = ?", (mac,)).fetchone()[0])  # fmt: skip
        rows = conn.execute(
            "SELECT content_hash, rel_path, rel_key, size, verified_at FROM replica "
            "WHERE dest_id = ?",
            (mac,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(h, ssd, rel, key, size, v) for h, rel, key, size, v in rows],
        )
    for _h, rel, _k, _s, _v in rows:
        dst = ssd_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((mac_root / rel).read_bytes())
    rig.ctx.config.backup.ssd_volume_uuid = "U-1"
    import sift.cleanup.manifest as manifest

    real = verify.usable_dests

    def both(ctx):
        return [*real(ctx), Dest(ssd, "ssd", ssd_root)]

    monkeypatch.setattr(verify, "usable_dests", both)
    monkeypatch.setattr(manifest, "usable_dests", both)
    return mac_root, ssd_root


@pytest.mark.parametrize("when", ["before_checks", "before_delete"])
def test_every_required_copy_is_checked(
    rig: Rig, tmp_path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """With an SSD required too, a damaged SSD copy alone stops the delete."""
    ids = camera(rig)[:2]
    mac_root, ssd_root = with_ssd_copies(rig, tmp_path, monkeypatch)
    rel0 = str(local_of(rig, ids[0]).relative_to(mac_root))
    if when == "before_checks":
        damage_same_size(ssd_root / rel0)
    run_id, counts = preflight(rig, ids)
    if when == "before_checks":
        assert results(rig, run_id)[ids[0]] == "backup_corrupt", results(rig, run_id)
        return
    assert counts == {"ok": 2}
    damage_same_size(ssd_root / rel0)
    done = run(rig, run_id)
    assert path_of(rig, ids[0]) not in deletes(rig) and done["deleted"] == 1


# -- 3. a phone edit that keeps size and mtime is caught before the delete -------------------


def test_a_phone_edit_keeping_size_and_mtime_is_not_deleted(rig: Rig) -> None:
    ids = camera(rig)[:3]
    run_id, counts = preflight(rig, ids)
    assert counts == {"ok": 3}
    path = path_of(rig, ids[0])
    entry = rig.transport.phone.entry(path)
    old = rig.transport.phone.read_range(path, 0, entry.size)
    rig.transport.phone.modify(path, bytes(b ^ 0x55 for b in old), entry.mtime)
    done = run(rig, run_id)
    assert path not in deletes(rig) and rig.transport.phone.entry(path) is not None
    assert done["deleted"] == 2


# -- 2. restore checks the source and the result ---------------------------------------------


def _deleted_with_archive(rig: Rig) -> tuple[list[int], int]:
    ids = camera(rig)[:2]
    run_id, _ = preflight(rig, ids)
    run(rig, run_id)
    mac = current_dest_ids(rig.ctx.db, rig.ctx.config)["mac"]
    return ids, mac


def test_archive_restore_refuses_a_damaged_backup(rig: Rig) -> None:
    ids, mac = _deleted_with_archive(rig)
    local = local_of(rig, ids[0])
    damage_same_size(local)
    rel = str(local).split(str(rig.root) + "/")[-1]
    out = archive_restore(rig.ctx, mac, [rel], rig.session)
    assert out["restored"] == 0 and out["failed"] == 1
    assert rig.transport.phone.entry(path_of(rig, ids[0])) is None  # nothing pushed
    with rig.ctx.db.read() as conn:
        assert conn.execute(
            "SELECT state, present FROM phone_file WHERE id = ?", (ids[0],)
        ).fetchone() == ("deleted", 0)


def test_archive_restore_removes_an_upload_the_phone_got_wrong(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids, mac = _deleted_with_archive(rig)
    local = local_of(rig, ids[0])
    rel = str(local).split(str(rig.root) + "/")[-1]
    real_push = rig.transport.push

    def bad_push(src, remote, mtime):  # the bytes arrive damaged
        real_push(src, remote, mtime)
        e = rig.transport.phone.entry(remote)
        data = rig.transport.phone.read_range(remote, 0, e.size)
        rig.transport.phone.modify(remote, bytes([data[0] ^ 1]) + data[1:], mtime)

    monkeypatch.setattr(rig.transport, "push", bad_push)
    out = archive_restore(rig.ctx, mac, [rel], rig.session)
    assert out["restored"] == 0 and out["failed"] == 1
    assert rig.transport.phone.entry(path_of(rig, ids[0])) is None  # its own upload removed


def test_quarantine_restore_to_phone_refuses_a_damaged_copy(rig: Rig) -> None:
    ids = whatsapp(rig, 2)
    with rig.ctx.db.tx() as conn:
        conn.executemany(
            "INSERT INTO label (content_hash, label, source, created_at) "
            "SELECT content_hash, 'junk', 'manual', 1 FROM phone_file WHERE id = ?",
            [(i,) for i in ids],
        )
    run_id, _ = preflight(rig, ids, source="cleanup", disposition="quarantine")
    run(rig, run_id)
    e = entries(rig.ctx)[0]
    mac_root = rig.root
    damage_same_size(mac_root / e.q_rel)
    from sift.cleanup.errors import RestoreRefused

    with pytest.raises(RestoreRefused):
        q_restore(rig.ctx, [e.id], "phone", rig.session)
    assert rig.transport.phone.entry(e.phone_path) is None


# -- 4. an interrupted quarantine restore is repaired, bytes and tracking --------------------


@pytest.mark.parametrize("collide", [False, True])
def test_a_crash_inside_quarantine_restore_is_repaired(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, collide: bool
) -> None:
    import sift.cleanup.quarantine as q

    ids = whatsapp(rig, 1)
    with rig.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO label (content_hash, label, source, created_at) "
            "SELECT content_hash, 'junk', 'manual', 1 FROM phone_file WHERE id = ?",
            (ids[0],),
        )
    run_id, _ = preflight(rig, ids, source="cleanup", disposition="quarantine")
    run(rig, run_id)
    e = entries(rig.ctx)[0]
    if collide:  # something already sits at the original place: restore picks a new name
        (rig.root / e.orig_rel).parent.mkdir(parents=True, exist_ok=True)
        (rig.root / e.orig_rel).write_bytes(b"another file")
    data = (rig.root / e.q_rel).read_bytes()
    real_move = q._move

    def move_then_crash(root, src, dst):
        real_move(root, src, dst)
        raise SystemExit("power cut after the move, before the database")

    monkeypatch.setattr(q, "_move", move_then_crash)
    with pytest.raises(SystemExit):
        q_restore(rig.ctx, [e.id], "backup")
    monkeypatch.setattr(q, "_move", real_move)
    reconcile(rig.ctx)  # what the next start does
    with rig.ctx.db.read() as conn:
        rel = conn.execute(
            "SELECT rel_path FROM replica WHERE content_hash = ?", (e.content_hash,)
        ).fetchone()[0]
    assert not rel.startswith(".quarantine/")
    assert (rig.root / rel).read_bytes() == data  # tracked where the bytes are
    assert entries(rig.ctx) == []  # restored, not lost
    if collide:
        assert (rig.root / e.orig_rel).read_bytes() == b"another file"


def _one_quarantined(rig: Rig):
    ids = whatsapp(rig, 1)
    with rig.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO label (content_hash, label, source, created_at) "
            "SELECT content_hash, 'junk', 'manual', 1 FROM phone_file WHERE id = ?",
            (ids[0],),
        )
    run_id, _ = preflight(rig, ids, source="cleanup", disposition="quarantine")
    return ids, run_id


def _tracked(rig: Rig, digest: str) -> str:
    with rig.ctx.db.read() as conn:
        return conn.execute(
            "SELECT rel_path FROM replica WHERE content_hash = ?", (digest,)
        ).fetchone()[0]


def test_a_crash_before_the_restore_move_keeps_it_quarantined(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sift.cleanup.quarantine as q

    _ids, run_id = _one_quarantined(rig)
    run(rig, run_id)
    e = entries(rig.ctx)[0]

    def crash(*a, **k):
        raise SystemExit("power cut before the move")

    monkeypatch.setattr(q, "_move", crash)
    with pytest.raises(SystemExit):
        q_restore(rig.ctx, [e.id], "backup")
    monkeypatch.undo()
    reconcile(rig.ctx)
    assert [x.id for x in entries(rig.ctx)] == [e.id]  # still quarantined, plan cleared
    assert _tracked(rig, e.content_hash) == e.q_rel and (rig.root / e.q_rel).is_file()
    q_restore(rig.ctx, [e.id], "backup")  # and a later restore works
    assert entries(rig.ctx) == [] and (rig.root / _tracked(rig, e.content_hash)).is_file()


@pytest.mark.parametrize("where", ["before_move", "after_move"])
def test_a_crash_while_quarantining_is_repaired(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    import sift.cleanup.quarantine as q

    ids, run_id = _one_quarantined(rig)
    digest_bytes = local_of(rig, ids[0]).read_bytes()
    real_move = q._move

    def crash(root, src, dst):
        if where == "after_move":
            real_move(root, src, dst)
        raise SystemExit("power cut")

    monkeypatch.setattr(q, "_move", crash)
    with pytest.raises(SystemExit):
        run(rig, run_id)
    monkeypatch.undo()
    reconcile(rig.ctx)
    with rig.ctx.db.read() as conn:
        digest = conn.execute(
            "SELECT content_hash FROM phone_file WHERE id = ?", (ids[0],)
        ).fetchone()[0]
    rel = _tracked(rig, digest)
    assert (rig.root / rel).read_bytes() == digest_bytes  # tracked where the bytes are
    assert rel.startswith(".quarantine/") == (where == "after_move")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/diagnostics"),
        ("post", "/api/logs/open"),
        ("get", "/api/archive/chat-backups"),
        ("post", "/api/archive/chat-backups/delete"),
        ("post", "/api/archive/restore"),
        ("post", "/api/quarantine/restore"),
        ("post", "/api/apps/uninstall"),
        ("put", "/api/settings"),
    ],
)
def test_sensitive_routes_need_the_token(rig: Rig, method: str, path: str) -> None:
    from tests.cleanup.test_browse import client

    c, _h = client(rig)
    r = c.get(path) if method == "get" else getattr(c, method)(path, json={})
    assert r.status_code in (401, 403), (path, r.status_code)


def test_a_quarantine_move_never_replaces_a_file(tmp_path) -> None:
    from sift.cleanup.quarantine import _move

    (tmp_path / "a.jpg").write_bytes(b"moving")
    (tmp_path / "b.jpg").write_bytes(b"already here")
    with pytest.raises(FileExistsError):
        _move(tmp_path, "a.jpg", "b.jpg")
    assert (tmp_path / "b.jpg").read_bytes() == b"already here"
    assert (tmp_path / "a.jpg").read_bytes() == b"moving"
