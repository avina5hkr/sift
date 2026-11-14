"""M14: health report, bug report, pre-reset checklist and restore helper (spec 10D)."""

import time
from pathlib import Path

import pytest

from sift.health import report as health
from tests.backup.test_confirm import backed_up
from tests.backup.test_run import Rig
from tests.cleanup.test_apps import DB, phone_with_apps
from tests.cleanup.test_browse import client

NOW = time.mktime((2026, 11, 14, 12, 0, 0, 0, 0, -1))


def areas(rig: Rig, version: str) -> dict[str, health.Area]:
    rig.transport.phone.android_version = version
    phone_with_apps(rig)
    rep = health.collect(rig.transport, rig.ctx.config, now=NOW)
    return {a.key: a for a in rep.areas}


def test_android_14_is_healthy(rig: Rig) -> None:
    a = areas(rig, "14")
    assert all(x.level == "ok" for x in a.values()), {k: x.verdicts for k, x in a.items()}
    assert "Security patch is from 2026-09-01." in a["system"].verdicts
    assert a["storage"].verdicts[0].startswith("Storage is 86% full")
    assert a["memory"].verdicts[1].startswith("Top 5 apps by memory: com.example.social")
    assert a["battery"].verdicts[0] == "Battery health good, temperature 31 C."
    assert a["network"].verdicts[:2] == ["SIM ready on LTE.", "Signal level 3 of 4."]
    assert health.NETWORK_ADVICE not in a["network"].verdicts
    assert a["apps"].facts["largest"][0][0] == "com.example.oldgame"  # third-party only


def test_android_15_is_full_hot_and_throttling(rig: Rig) -> None:
    a = areas(rig, "15")
    assert a["system"].level == "warn" and "11 months old" in a["system"].verdicts[1]
    assert a["storage"].level == "warn" and "which often causes lag" in a["storage"].verdicts[0]
    assert a["battery"].level == "warn" and "hot" in a["battery"].verdicts[1]
    assert a["heat"].level == "bad" and "throttling (moderate)" in a["heat"].verdicts[0]
    assert a["network"].level == "bad" and health.NETWORK_ADVICE in a["network"].verdicts


def test_android_16_has_no_service_and_says_a_reset_may_not_fix_it(rig: Rig) -> None:
    a = areas(rig, "16")
    net = a["network"]
    assert net.level == "bad"
    assert "SIM ready in slot 2 on NR." in net.verdicts
    assert "Calls: out of service." in net.verdicts
    assert "Wi-Fi is off." in net.verdicts and net.verdicts[-1] == health.NETWORK_ADVICE
    assert a["storage"].level == "ok"  # the wrapped df line is read
    assert not any("fault" in v.lower() for x in a.values() for v in x.verdicts)


@pytest.mark.parametrize(
    "kind", ["props", "df", "meminfo", "top", "battery", "thermal", "telephony"]
)
def test_a_missing_probe_leaves_the_rest_of_the_report(rig: Rig, kind: str) -> None:
    rig.transport.phone.health[kind] = None
    phone_with_apps(rig)
    rep = health.collect(rig.transport, rig.ctx.config, now=NOW)
    assert kind in rep.unavailable
    levels = {a.key: a.level for a in rep.areas}
    assert sum(lv != "unavailable" for lv in levels.values()) >= 5


def test_garbage_from_every_probe_is_unavailable_not_an_error(rig: Rig) -> None:
    rig.transport.phone.health = {k: "\x00<<garbage>>" for k in health.h.PROBES}
    rep = health.collect(rig.transport, rig.ctx.config, now=NOW)
    assert all(a.level == "unavailable" for a in rep.areas)


def test_only_read_only_commands_run(rig: Rig) -> None:
    phone_with_apps(rig)
    health.collect(rig.transport, rig.ctx.config, now=NOW)
    assert not [e for e in rig.transport.journal if e.mutating]


def test_run_export_and_bug_report_through_the_api(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    phone_with_apps(rig)
    c, h = client(rig)
    assert c.get("/api/health/report", headers=h).json() == {"report": None}
    assert c.post("/api/health/export", headers=h).status_code == 409  # nothing to export
    assert c.post("/api/health/run", headers=h).status_code == 200
    assert rig.ctx.tasks.wait(60) and rig.ctx.tasks.current().state == "done"
    rep = c.get("/api/health/report", headers=h).json()["report"]
    assert [a["key"] for a in rep["areas"]] == [
        "system", "storage", "memory", "battery", "heat", "network", "apps"
    ]  # fmt: skip
    out = c.post("/api/health/export", headers=h).json()
    text = Path(out["paths"][0]).read_text()
    assert text.startswith("# Phone health report") and "## Storage (OK)" in text
    assert Path(out["paths"][0]).parent == rig.root / ".sift" / "reports"
    assert c.post("/api/health/bugreport", headers=h).status_code == 200
    assert rig.ctx.tasks.wait(60)
    paths = rig.ctx.tasks.current().result["paths"]
    assert Path(paths[0]).read_bytes() == rig.transport.phone.bugreport
    assert not list((rig.root / ".sift" / "reports").glob(".*part*"))


def test_a_failed_bug_report_saves_nothing(rig: Rig) -> None:
    backed_up(rig, ["Camera"])
    rig.transport.phone.bugreport = None
    c, h = client(rig)
    c.post("/api/health/bugreport", headers=h)
    assert rig.ctx.tasks.wait(60) and rig.ctx.tasks.current().state == "failed"
    assert not list((rig.root / ".sift" / "reports").glob("*.zip"))


def test_report_dir_cannot_leave_the_backup_folder(rig: Rig) -> None:
    from sift.core.files import report_folder

    assert report_folder(rig.root, "../../etc") == rig.root / ".sift" / "reports"
    assert report_folder(rig.root, "/tmp/x") == rig.root / ".sift" / "reports"
    assert report_folder(rig.root, "reports/phone") == rig.root / "reports" / "phone"


VCF = b"BEGIN:VCARD\nFN:A\nEND:VCARD\nBEGIN:VCARD\nFN:B\nEND:VCARD\n"


def checklist(c, h) -> dict[int, dict]:
    return {i["id"]: i for i in c.get("/api/reset-checklist", headers=h).json()["items"]}


def test_the_checklist_follows_the_backup_and_the_ticks(rig: Rig) -> None:
    phone_with_apps(rig)
    c, h = client(rig)
    items = checklist(c, h)
    assert len(items) == 10 and not any(i["green"] for i in items.values())
    assert items[3]["auto_detail"].startswith("No backed-up .vcf")
    # contacts: a .vcf on the phone, backed up; the phone has 2 contacts
    rig.transport.phone.add_file("/sdcard/contacts.vcf", VCF, mtime=1_700_000_000)
    rig.transport.phone.contacts = 2
    now = int(time.time())
    rig.transport.phone.add_file(f"{DB}/msgstore.db.crypt15", b"chats", mtime=now - 600)
    backed_up(rig, [s.name for s in rig.ctx.config.backup.sets if s.enabled])
    c.post("/api/apps/export", headers=h, json={})  # app list saved
    items = checklist(c, h)
    assert items[3]["green"] and "2 contacts; the phone has 2" in items[3]["auto_detail"]
    assert items[9]["green"]
    assert items[2]["auto_ok"] and not items[2]["green"]  # the Drive backup still needs a tick
    ticked = c.put(
        "/api/reset-checklist/2", headers=h, json={"ticked": True, "note": "key in the safe"}
    ).json()
    assert {i["id"]: i for i in ticked["items"]}[2]["green"]
    assert c.put("/api/reset-checklist/1", headers=h, json={"ticked": True}).status_code == 422
    assert c.put("/api/reset-checklist/11", headers=h, json={"ticked": True}).status_code == 404
    rig.transport.phone.contacts = 5  # more contacts than the export holds
    assert not checklist(c, h)[3]["green"]
    out = c.post("/api/reset-checklist/export", headers=h).json()
    assert "- [x] 2. WhatsApp" in out["markdown"] and "key in the safe" in out["markdown"]
    assert Path(out["paths"][0]).exists()


def test_the_backup_item_needs_every_set_confirmed(rig: Rig) -> None:
    from sift.backup.confirm import confirm
    from sift.backup.run import usable_dests

    for s in rig.ctx.config.backup.sets:
        s.enabled = s.name == "Camera"
    backed_up(rig, ["Camera"])
    c, h = client(rig)
    assert "Camera (not confirmed)" in checklist(c, h)[1]["auto_detail"]
    confirm(rig.ctx.db, rig.ctx.config, 1, "Camera", rig.transport, dests=usable_dests(rig.ctx))
    assert checklist(c, h)[1]["green"]


def test_restore_helper_pushes_the_contacts_beside_never_over(rig: Rig) -> None:
    phone_with_apps(rig)
    rig.transport.phone.add_file("/sdcard/contacts.vcf", VCF, mtime=1_700_000_000)
    backed_up(rig, ["Loose files"])
    c, h = client(rig)
    c.post("/api/apps/export", headers=h, json={})
    helper = c.get("/api/reset-checklist/restore", headers=h).json()
    assert helper["contacts"] == {"phone_path": "/sdcard/contacts.vcf", "cards": 2}
    assert "com.example.oldgame" in {a["package"] for a in helper["reinstall"]["apps"]}
    assert c.post("/api/reset-checklist/restore/contacts", headers=h, json={}).status_code == 409

    def push() -> str:
        assert (
            c.post(
                "/api/reset-checklist/restore/contacts", headers=h, json={"confirm": True}
            ).status_code
            == 200
        )
        assert rig.ctx.tasks.wait(60)
        return rig.ctx.tasks.current().result["phone_path"]

    assert push() == "/sdcard/Download/contacts.vcf"
    assert push() == "/sdcard/Download/contacts_restored.vcf"
    assert rig.transport.phone.entry("/sdcard/Download/contacts.vcf") is not None


def test_a_sim_in_slot_two_is_read_from_its_own_block(rig: Rig) -> None:
    from sift.device import fake_health as fx

    phone = rig.transport.phone
    phone.android_version = "16"
    phone.health["telephony"] = "x\n" + fx.radio_block(0, fx.OUT, 0) + fx.radio_block(1, fx.IN, 4)
    a = {x.key: x for x in health.collect(rig.transport, rig.ctx.config, now=NOW).areas}
    assert a["network"].level == "ok" and "Signal level 4 of 4." in a["network"].verdicts


@pytest.mark.parametrize(
    ("sims", "voice", "expect"),
    [("ABSENT", "OUT_OF_SERVICE", "No SIM is inserted."),
     ("READY", "POWER_OFF", "Airplane mode is on (the mobile radio is off).")],
)  # fmt: skip
def test_no_sim_or_airplane_mode_gets_no_reset_advice(
    rig: Rig, sims: str, voice: str, expect: str
) -> None:
    from sift.device import fake_health as fx

    phone = rig.transport.phone
    phone.health["props"] = fx.props("14").replace("[READY]", f"[{sims}]")
    phone.health["telephony"] = fx.radio_block(
        0, fx.IN.replace("0(IN_SERVICE)", f"3({voice})", 1), 0
    )
    net = {x.key: x for x in health.collect(rig.transport, rig.ctx.config, now=NOW).areas}[
        "network"
    ]
    assert expect in net.verdicts and health.NETWORK_ADVICE not in net.verdicts


def test_files_are_placed_without_hard_links_and_never_replace(tmp_path: Path, monkeypatch) -> None:
    """exFAT has no hard links; the no-replace rename (and its fallback) is used instead."""
    import sift.backup.staging as staging
    from sift.core.files import write_new, write_to_each

    monkeypatch.setattr("os.link", lambda *a, **k: (_ for _ in ()).throw(OSError(45, "no")))
    monkeypatch.setattr(staging, "_renamex_np", None)  # the lock + lstat + rename fallback
    first = write_new(tmp_path / "r.md", b"one")
    second = write_new(tmp_path / "r.md", b"two")
    assert (first.read_bytes(), second.name, second.read_bytes()) == (b"one", "r-1.md", b"two")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r-1.md", "r.md"]
    blocked = tmp_path / "ro"
    blocked.write_text("a file where a folder should be")
    written, failed = write_to_each([tmp_path / "ok", blocked], "reports", "x.md", b"x")
    assert len(written) == 1 and len(failed) == 1
