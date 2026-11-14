"""Hardening follow-up: a restore upload only ever removes or replaces the file it created.

The reviewed race: the free name was chosen first and the upload went straight to it, so a file
that appeared at that name meanwhile (the camera, another app) was removed by the failed-upload
cleanup. Uploads now land under Sift's own random temporary name and are moved into place with a
no-replace rename."""

import pytest

from sift.cleanup.archive import _remove_own_upload, push_one
from sift.cleanup.archive import restore as archive_restore
from sift.cleanup.errors import RestoreRefused
from sift.cleanup.quarantine import entries
from sift.cleanup.quarantine import restore as q_restore
from sift.device.transport import NameTaken, RemoteError
from tests.backup.test_run import Rig
from tests.cleanup.test_delete import local_of, path_of, preflight, run, whatsapp
from tests.hardening.test_delete_restore_safety import _deleted_with_archive

OTHER = b"someone else's file, not Sift's"


def temps(rig: Rig) -> list[str]:
    return [p for p in rig.transport.phone.files if ".sift-upload-" in p]


def bytes_at(rig: Rig, path: str) -> bytes | None:
    e = rig.transport.phone.entry(path)
    return None if e is None else rig.transport.phone.read_range(path, 0, e.size)


def racing_push(rig: Rig, final: str, how: str):
    """A push during which another file appears at the restore's final name."""
    real_push = rig.transport.push

    def push(src, remote, mtime):
        rig.transport.phone.add_file(final, OTHER, mtime=5)
        if how == "fails":
            raise RemoteError("the phone refused", remote)
        real_push(src, remote, mtime)
        if how == "damaged":
            data = bytes_at(rig, remote)
            assert data is not None
            rig.transport.phone.modify(remote, bytes([data[0] ^ 1]) + data[1:], mtime)

    return push


@pytest.mark.parametrize("how", ["ok", "fails", "damaged"])
def test_archive_restore_never_touches_a_file_that_appears_at_its_name(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    ids, mac = _deleted_with_archive(rig)
    local = local_of(rig, ids[0])
    rel = str(local).split(str(rig.root) + "/")[-1]
    final = path_of(rig, ids[0])
    monkeypatch.setattr(rig.transport, "push", racing_push(rig, final, how))
    out = archive_restore(rig.ctx, mac, [rel], rig.session)
    assert bytes_at(rig, final) == OTHER  # the other file is never removed or replaced
    assert temps(rig) == []
    if how == "ok":
        assert out["restored"] == 1
        moved = final.rsplit(".", 1)[0] + "_restored." + final.rsplit(".", 1)[1]
        assert bytes_at(rig, moved) == local.read_bytes()
    else:
        assert out["restored"] == 0 and out["failed"] == 1


@pytest.mark.parametrize("how", ["ok", "fails", "damaged"])
def test_quarantine_restore_never_touches_a_file_that_appears_at_its_name(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
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
    assert e.phone_path is not None
    monkeypatch.setattr(rig.transport, "push", racing_push(rig, e.phone_path, how))
    if how == "ok":
        q_restore(rig.ctx, [e.id], "phone", rig.session)
    else:
        with pytest.raises((RestoreRefused, RemoteError)):
            q_restore(rig.ctx, [e.id], "phone", rig.session)
        assert entries(rig.ctx)[0].id == e.id  # still quarantined, still tracked
    assert bytes_at(rig, e.phone_path) == OTHER
    assert temps(rig) == []


def test_a_name_taken_during_the_move_goes_to_the_next_free_name(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids, mac = _deleted_with_archive(rig)
    local = local_of(rig, ids[0])
    rel = str(local).split(str(rig.root) + "/")[-1]
    final = path_of(rig, ids[0])
    real_rename = rig.transport.rename_new

    def rename(src, dst):  # the file appears between the free-name check and the move
        if dst == final:
            rig.transport.phone.add_file(final, OTHER, mtime=5)
        real_rename(src, dst)

    monkeypatch.setattr(rig.transport, "rename_new", rename)
    assert archive_restore(rig.ctx, mac, [rel], rig.session)["restored"] == 1
    assert bytes_at(rig, final) == OTHER
    assert temps(rig) == []


def test_push_one_never_replaces_a_file_that_appears(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    local = tmp_path / "contacts.vcf"
    local.write_bytes(b"BEGIN:VCARD\nEND:VCARD\n")
    final = "/sdcard/Download/contacts.vcf"
    monkeypatch.setattr(rig.transport, "push", racing_push(rig, final, "ok"))
    where = push_one(rig.session, local, final, 1_700_000_000)
    assert where == "/sdcard/Download/contacts_restored.vcf"
    assert bytes_at(rig, final) == OTHER and bytes_at(rig, where) == local.read_bytes()
    assert temps(rig) == []


def test_the_fake_phone_never_writes_over_a_file(rig: Rig, tmp_path) -> None:
    """The fake matches AdbTransport: push and the rename refuse a taken name."""
    local = tmp_path / "a.jpg"
    local.write_bytes(b"new")
    rig.transport.phone.add_file("/sdcard/DCIM/a.jpg", OTHER)
    rig.transport.phone.add_file("/sdcard/DCIM/b.jpg", b"b")
    with pytest.raises(RemoteError, match="already exists"):
        rig.transport.push(local, "/sdcard/DCIM/a.jpg", 1)
    with pytest.raises(NameTaken):
        rig.transport.rename_new("/sdcard/DCIM/b.jpg", "/sdcard/DCIM/a.jpg")
    assert bytes_at(rig, "/sdcard/DCIM/a.jpg") == OTHER
    assert bytes_at(rig, "/sdcard/DCIM/b.jpg") == b"b"


def test_cleanup_refuses_a_name_sift_did_not_make(rig: Rig) -> None:
    rig.transport.phone.add_file("/sdcard/DCIM/a.jpg", OTHER)
    with pytest.raises(AssertionError):
        _remove_own_upload(rig.transport, "/sdcard/DCIM/a.jpg")
    assert bytes_at(rig, "/sdcard/DCIM/a.jpg") == OTHER
