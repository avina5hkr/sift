"""Owner request: see the saved WhatsApp chat backups by date and delete older ones."""

import time
from pathlib import Path

from tests.backup.test_confirm import backed_up
from tests.backup.test_run import Rig
from tests.cleanup.test_apps import DB
from tests.cleanup.test_browse import client

CHAT = ["WhatsApp chat backups"]


def three_versions(rig: Rig) -> None:
    """Night 1 and night 2 of msgstore.db.crypt15 (same phone path), plus a dated file."""
    phone, now = rig.transport.phone, int(time.time())
    phone.add_file(f"{DB}/msgstore.db.crypt15", b"night one", mtime=now - 2 * 86400)
    phone.add_file(f"{DB}/msgstore-2026-11-01.1.db.crypt15", b"dated", mtime=now - 13 * 86400)
    backed_up(rig, CHAT)
    phone.modify(f"{DB}/msgstore.db.crypt15", b"night two!", now - 86400)
    backed_up(rig, CHAT)


def listing(c, h) -> dict[str, dict]:
    return {
        x["content_hash"]: x for x in c.get("/api/archive/chat-backups", headers=h).json()["copies"]
    }


def test_old_chat_backups_are_listed_by_date_and_can_be_deleted(rig: Rig) -> None:
    three_versions(rig)
    c, h = client(rig)
    copies = listing(c, h)
    assert len(copies) == 3 and all(x["app"] == "WhatsApp" for x in copies.values())
    newest = [x for x in copies.values() if x["newest"]]
    assert (
        len(newest) == 1 and not newest[0]["deletable"] and newest[0]["size"] == len(b"night two!")
    )
    dated = next(x for x in copies.values() if x["name"].startswith("msgstore-2026"))
    assert dated["on_phone"] and not dated["deletable"]  # still on the phone: kept
    old = next(x for x in copies.values() if x["deletable"])
    assert old["size"] == len(b"night one") and old["drives"] == ["mac"]
    files_before = {p for p in rig.root.rglob("msgstore*")}
    assert (
        c.post(
            "/api/archive/chat-backups/delete", headers=h, json={"hashes": [old["content_hash"]]}
        ).status_code
        == 409
    )  # not confirmed
    out = c.post("/api/archive/chat-backups/delete", headers=h,
                 json={"hashes": [old["content_hash"]], "confirm": True}).json()  # fmt: skip
    assert out == {"deleted": 1, "failed": 0, "freed_bytes": len(b"night one")}
    gone = files_before - {p for p in rig.root.rglob("msgstore*")}
    assert len(gone) == 1  # only that copy's file
    assert old["content_hash"] not in listing(c, h)
    backed_up(rig, CHAT)  # the next backup does not bring it back
    assert old["content_hash"] not in listing(c, h)


def test_the_newest_or_one_on_the_phone_is_refused_and_nothing_is_deleted(rig: Rig) -> None:
    three_versions(rig)
    c, h = client(rig)
    copies = listing(c, h)
    kept = [x["content_hash"] for x in copies.values() if not x["deletable"]]
    old = [x["content_hash"] for x in copies.values() if x["deletable"]]
    files = sorted(p.read_bytes() for p in rig.root.rglob("msgstore*"))
    for hashes in ([kept[0]], [old[0], kept[1]], ["0" * 64]):
        r = c.post(
            "/api/archive/chat-backups/delete", headers=h, json={"hashes": hashes, "confirm": True}
        )
        assert r.status_code == 409
    assert sorted(p.read_bytes() for p in rig.root.rglob("msgstore*")) == files


def test_other_folders_named_backups_are_not_chat_backups(rig: Rig) -> None:
    phone = rig.transport.phone
    phone.add_file("/sdcard/Documents/WhatsApp/Backups/notes.txt", b"mine", mtime=1_700_000_000)
    backed_up(rig, ["Documents"])
    c, h = client(rig)
    assert listing(c, h) == {}


def test_a_copy_on_an_unplugged_drive_blocks_the_delete(rig: Rig) -> None:
    three_versions(rig)
    c, h = client(rig)
    old = next(x for x in listing(c, h).values() if x["deletable"])
    with rig.ctx.db.tx() as conn:  # pretend the SSD (not plugged in) also holds a copy
        conn.execute(
            "INSERT INTO destination (kind, root_path, volume_uuid) "
            "VALUES ('ssd', '/Volumes/Gone', 'u')"
        )
        ssd = conn.execute("SELECT id FROM destination WHERE kind = 'ssd'").fetchone()[0]
        conn.execute(
            "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, verified_at) "
            "SELECT content_hash, ?, rel_path, rel_key, size, verified_at FROM replica "
            "WHERE content_hash = ?",
            (ssd, old["content_hash"]),
        )
    r = c.post("/api/archive/chat-backups/delete", headers=h,
               json={"hashes": [old["content_hash"]], "confirm": True})  # fmt: skip
    assert r.status_code == 409 and "Plug in" in r.json()["message"]
    assert any(Path(p).exists() for p in rig.root.rglob("msgstore*"))
