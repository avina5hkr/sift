"""M9: Settings pages, the Logs screen and diagnostics export."""

import json
import zipfile
from pathlib import Path

import pytest

from sift.core.config import load_config
from tests.backup.test_run import Rig
from tests.cleanup.test_browse import client


def test_settings_save_validate_and_reload(rig: Rig) -> None:
    c, h = client(rig)
    pages = c.get("/api/settings", headers=h).json()["pages"]
    assert set(pages) == {
        "general",
        "phone",
        "destinations",
        "backup",
        "cleanup",
        "models",
        "advanced",
    }
    wifi = next(f for f in pages["phone"] if f["key"] == "allow_delete_over_wifi")
    assert wifi["value"] is False and wifi["default"] is False
    r = c.put("/api/settings", headers=h, json={"changes": [
        {"section": "phone", "key": "allow_delete_over_wifi", "value": True},
        {"section": "cleanup", "key": "min_age_days", "value": 14},
    ]})  # fmt: skip
    assert r.status_code == 200
    assert rig.ctx.config.phone.allow_delete_over_wifi and rig.ctx.config.cleanup.min_age_days == 14
    saved, _ = load_config(rig.ctx.paths.config)
    assert saved.cleanup.min_age_days == 14 and saved.phone.allow_delete_over_wifi


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"section": "phone", "key": "parallel_reads_usb", "value": 0}, "invalid"),
        ({"section": "backup", "key": "mac_root", "value": "/tmp/elsewhere"}, "not_editable"),
        ({"section": "backup", "key": "ssd_volume_uuid", "value": "x"}, "not_editable"),
        ({"section": "cleanup", "key": "min_age_days", "value": "soon"}, "invalid"),
    ],
)
def test_bad_or_protected_changes_change_nothing(rig: Rig, change: dict, code: str) -> None:
    c, h = client(rig)
    before = rig.ctx.config.model_dump()
    good = {"section": "cleanup", "key": "quarantine_days", "value": 45}
    r = c.put("/api/settings", headers=h, json={"changes": [good, change]})
    assert r.status_code == 422 and r.json()["code"] == code
    assert rig.ctx.config.model_dump() == before  # all or nothing


def test_the_never_delete_floor_survives_settings(rig: Rig) -> None:
    c, h = client(rig)
    c.put("/api/settings", headers=h, json={"changes": [
        {"section": "cleanup", "key": "never_delete", "value": ["/sdcard/Mine"]}
    ]})  # fmt: skip
    assert "/sdcard/Android/data" in rig.ctx.config.cleanup.never_delete


def write_log(rig: Rig, lines: list[dict]) -> None:
    rig.ctx.paths.logs.mkdir(parents=True, exist_ok=True)
    (rig.ctx.paths.logs / "sift.log").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\nnot json\n"
    )


def test_logs_newest_first_with_filters(rig: Rig) -> None:
    write_log(rig, [
        {"ts": 1, "level": "INFO", "area": "backup", "message": "copied 3 files"},
        {"ts": 2, "level": "DEBUG", "area": "device", "message": "noise"},
        {"ts": 3, "level": "WARNING", "area": "device.adb", "message": "phone slow"},
        {"ts": 4, "level": "ERROR", "area": "cleanup", "message": "delete failed"},
    ])  # fmt: skip
    c, h = client(rig)

    def msgs(**q):
        return [e["message"] for e in c.get("/api/logs", headers=h, params=q).json()["entries"]]

    assert msgs() == ["delete failed", "phone slow", "copied 3 files"]
    assert msgs(level="WARNING") == ["delete failed", "phone slow"]
    assert msgs(area="device", level="DEBUG") == ["phone slow", "noise"]
    assert msgs(q="COPIED") == ["copied 3 files"]


def test_diagnostics_zip_hides_paths_unless_asked(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sift.server.routes.logs as logs

    monkeypatch.setattr(logs, "downloads_dir", lambda: tmp_path / "dl")
    root = str(Path(rig.ctx.config.backup.mac_root).expanduser())
    message = f"copied /sdcard/DCIM/Camera/IMG_1.jpg to {root}/Phone/x.jpg"
    write_log(rig, [{"ts": 1, "level": "INFO", "area": "backup", "message": message}])
    c, h = client(rig)
    out = c.post("/api/diagnostics", headers=h, json={}).json()
    with zipfile.ZipFile(out["path"]) as z:
        names = z.namelist()
        text = z.read("logs/sift.log").decode()
        about = json.loads(z.read("about.json"))
    assert "about.json" in names and about["schema_version"] >= 13
    assert "IMG_1" not in text and root not in text and "<phone path>" in text
    full = c.post("/api/diagnostics", headers=h, json={"include_paths": True}).json()
    with zipfile.ZipFile(full["path"]) as z:
        assert "IMG_1.jpg" in z.read("logs/sift.log").decode()
    assert full["path"] != out["path"]


def test_redaction_hides_names_with_spaces_serials_and_drives(rig: Rig) -> None:
    from sift.server.routes.logs import redact

    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE device SET model = 'Galaxy Test 9', label = 'Avi phone'")
    rig.ctx.config.backup.ssd_volume_uuid = "1234-ABCD-UUID"
    line = (
        '{"message": "copied /sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/'
        "IMG-2024 family.jpg from FAKE-0001 (Galaxy Test 9) adb-R5CT12-abc to "
        '/Volumes/My SSD/Sift/x.jpg, uuid 1234-ABCD-UUID"}'
    )
    out = redact(line, rig.ctx)
    for secret in ("family", "IMG-2024", "FAKE-0001", "Galaxy Test 9", "R5CT12", "My SSD",
                   "1234-ABCD-UUID"):  # fmt: skip
        assert secret not in out, (secret, out)


def test_starter_teaching_loads_once_on_a_fresh_install(rig: Rig, tmp_path: Path) -> None:
    from sift.cleanup.labels_io import export, load_starter

    starter = tmp_path / "starter.json"
    starter.write_text(json.dumps({"format": "sift-teaching", "version": 1, "labels": [
        {"hash": "a" * 64, "label": "junk", "tag": None},
        {"hash": "b" * 64, "label": "keep", "tag": None},
    ]}))  # fmt: skip
    assert load_starter(rig.ctx.db, starter)["read"] == 2
    assert len(export(rig.ctx.db)["labels"]) == 2
    with rig.ctx.db.tx() as conn:
        conn.execute("DELETE FROM label_import")
    assert load_starter(rig.ctx.db, starter) is None  # once only, even if emptied later
    assert load_starter(rig.ctx.db, tmp_path / "missing.json") is None
