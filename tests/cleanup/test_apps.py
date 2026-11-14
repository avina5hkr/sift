"""M13: the Apps tab. Parsers, the app list, protection (P10, I10) and the actions."""

import time
from pathlib import Path

import pytest

from sift.device.apps import parse_diskstats, parse_packages, parse_usagestats, valid_package
from sift.device.fake_phone import FakeApp
from tests.backup.test_confirm import backed_up
from tests.backup.test_run import Rig
from tests.cleanup.test_browse import client

DB = "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases"


def test_parsers_read_what_they_can() -> None:
    assert parse_packages(
        "package:com.example.notes  installer=com.android.vending\n"
        "package:org.test.app  installer=null\nnoise"
    ) == {"com.example.notes": "com.android.vending", "org.test.app": None}
    stats = parse_diskstats(
        'App Size: 1\nPackage Names: ["com.a.b","com.c.d"]\nApp Sizes: [100,200]\n'
        "App Data Sizes: [10,20]\nCache Sizes: [1]\n"
    )
    assert stats == {"com.a.b": (100, 10, 1), "com.c.d": (200, 20, None)}
    used = parse_usagestats(
        '    package=com.a.b totalTimeUsed="0:01" lastTimeUsed="2024-03-01 10:11:12"\n'
        '    package=com.a.b totalTimeUsed="0:01" lastTimeUsed="1709280000000"\n'
        "garbage line"
    )
    assert set(used) == {"com.a.b"} and used["com.a.b"] >= 1709280000
    assert parse_packages("") == {} and parse_diskstats("nothing") == {}
    assert valid_package("com.whatsapp") and not valid_package("com.x; rm -rf /")


def phone_with_apps(rig: Rig) -> None:
    now = int(time.time())
    rig.transport.phone.apps.update(
        {
            "com.whatsapp": FakeApp(last_used=now),
            "com.google.android.apps.authenticator2": FakeApp(last_used=now),
            "com.example.oldgame": FakeApp(app_bytes=900_000_000, last_used=now - 200 * 86400),
            "com.example.sideload": FakeApp(installer=None, last_used=now - 5 * 86400,
                                            apk_paths=("/data/app/x/base.apk",)),
        }
    )  # fmt: skip


def test_the_app_list_with_sizes_and_suggestions(rig: Rig) -> None:
    phone_with_apps(rig)
    c, h = client(rig)
    body = c.get("/api/apps", headers=h).json()
    by = {a["package"]: a for a in body["apps"]}
    assert by["com.whatsapp"]["protected"] and by["com.whatsapp"]["name"] == "WhatsApp"
    assert (
        by["com.example.oldgame"]["unused"]
        and by["com.example.oldgame"]["app_bytes"] == 900_000_000
    )
    assert not by["com.example.sideload"]["from_play_store"]
    assert body["suggested"] == ["com.example.oldgame"]  # unused, unprotected
    assert all(body["probes"].values())


def act(c, h, path: str, packages: list[str], **extra):
    return c.post(
        f"/api/apps/{path}", headers=h, json={"packages": packages, "confirm": True, **extra}
    )


def test_p10_whatsapp_unlocks_only_with_a_fresh_backup_a_tick_and_its_name(rig: Rig) -> None:
    backed_up(rig, ["Camera"])  # a mounted backup folder for the app list
    phone_with_apps(rig)
    c, h = client(rig)
    phone = rig.transport.phone
    wa = ["com.whatsapp"]

    def uninstall(**unlock):
        return act(c, h, "uninstall", wa, action="uninstall", unlock={"com.whatsapp": unlock})

    assert uninstall(backup_done=True, typed_name="WhatsApp").status_code == 409  # no backup
    phone.add_file(f"{DB}/msgstore.db.crypt15", b"old", mtime=int(time.time()) - 48 * 3600)
    assert uninstall(backup_done=True, typed_name="WhatsApp").status_code == 409  # too old
    phone.add_file(f"{DB}/msgstore-2026-01-01.1.db.crypt15", b"new", mtime=int(time.time()) - 600)
    assert uninstall(backup_done=False, typed_name="WhatsApp").status_code == 409  # no tick
    assert uninstall(backup_done=True, typed_name="Whats App").status_code == 409  # wrong name
    for clear in (act(c, h, "clear-data", wa, action="clear_data"),):
        assert clear.status_code == 409  # clear data is protected the same way
    assert not [e for e in rig.transport.journal if e.op in ("uninstall", "clear_data")]
    ok = uninstall(backup_done=True, typed_name="whatsapp")
    assert ok.status_code == 200 and ok.json()["done"] == wa
    assert ok.json()["app_list"] and Path(ok.json()["app_list"][0]).read_text().startswith(
        "name,package"
    )
    assert "com.whatsapp" not in phone.apps


def test_p10_an_authenticator_needs_an_explicit_unprotect(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    phone_with_apps(rig)
    c, h = client(rig)
    auth = "com.google.android.apps.authenticator2"
    for path, action in (("uninstall", "uninstall"), ("clear-data", "clear_data")):
        refused = act(c, h, path, [auth], action=action)
        assert refused.status_code == 409 and refused.json()["code"] == "protected"
    pre = c.post(
        "/api/apps/preflight", headers=h, json={"packages": [auth], "action": "uninstall"}
    ).json()
    assert auth in pre["refused"]
    assert c.post("/api/apps/unprotect", headers=h, json={"package": auth}).status_code == 409
    assert c.post(
        "/api/apps/unprotect", headers=h, json={"package": auth, "confirm": True}
    ).json() == {"unprotected": True}
    assert act(c, h, "clear-data", [auth], action="clear_data").json()["done"] == [auth]
    c.post("/api/apps/unprotect", headers=h, json={"package": auth, "protect_again": True})
    assert act(c, h, "uninstall", [auth], action="uninstall").status_code == 409
    assert (
        c.post(
            "/api/apps/unprotect", headers=h, json={"package": "com.whatsapp", "confirm": True}
        ).status_code
        == 409
    )  # WhatsApp unlocks only with its chat backup


def test_ordinary_apps_need_a_confirm_and_a_backup_folder_for_the_list(rig: Rig) -> None:
    phone_with_apps(rig)
    c, h = client(rig)
    game = ["com.example.oldgame"]
    assert (
        c.post(
            "/api/apps/uninstall", headers=h, json={"packages": game, "action": "uninstall"}
        ).status_code
        == 409
    )  # not confirmed
    rig.ctx.config.backup.mac_root = "/nonexistent/backup"  # nowhere to save the app list
    refused = act(c, h, "uninstall", game, action="uninstall")
    assert refused.status_code == 409 and refused.json()["code"] == "no_backup_drive"
    assert "com.example.oldgame" in rig.transport.phone.apps
    bad = act(c, h, "uninstall", ["com.x; reboot"], action="uninstall")
    assert bad.status_code == 409 and not [e for e in rig.transport.journal if e.op == "uninstall"]


def test_trim_caches_and_export_with_apks(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    phone_with_apps(rig)
    rig.transport.phone.add_file("/data/app/x/base.apk", b"APK bytes", mtime=1_600_000_000)
    c, h = client(rig)
    assert c.post("/api/apps/trim-caches", headers=h, json={}).status_code == 409
    assert c.post("/api/apps/trim-caches", headers=h, json={"confirm": True}).json() == {
        "done": True
    }
    assert all(a.cache_bytes == 0 for a in rig.transport.phone.apps.values())
    out = c.post("/api/apps/export", headers=h, json={"apks": ["com.example.sideload"]}).json()
    assert out["apks"] == {"saved": 1, "failed": 0}
    root = Path(out["app_list"][0]).parent
    assert (root / "apks" / "com.example.sideload" / "base.apk").read_bytes() == b"APK bytes"
    again = c.post("/api/apps/export", headers=h, json={"apks": ["com.example.sideload"]}).json()
    assert again["apks"]["saved"] == 1  # the existing APK is kept, not replaced


@pytest.mark.parametrize("kind", ["packages", "diskstats", "usagestats", "versions"])
def test_a_probe_that_fails_is_only_unavailable(rig: Rig, kind: str) -> None:
    phone_with_apps(rig)
    real = rig.transport.apps_probe
    rig.transport.apps_probe = lambda k: None if k == kind else real(k)  # type: ignore[method-assign]
    c, h = client(rig)
    body = c.get("/api/apps", headers=h)
    assert body.status_code == 200 and body.json()["probes"][kind] is False
