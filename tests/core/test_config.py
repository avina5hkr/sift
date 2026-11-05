from pathlib import Path

import pytest
from pydantic import ValidationError

from sift.core.config import BackupSet, Config, load_config, save_config


def test_missing_file_gives_spec_defaults(tmp_path: Path) -> None:
    cfg, warnings = load_config(tmp_path / "config.toml")
    assert warnings == []
    assert cfg.phone.adb_server_port == 5037
    assert cfg.backup.adopt_mode == "hash"
    assert cfg.backup.confirmation_valid_days == 30
    assert cfg.cleanup.delete_reminder_days == 0
    assert not hasattr(cfg.backup, "roots")
    assert cfg.phone.parallel_reads_usb == 3
    assert cfg.phone.parallel_reads_wifi == 2
    assert cfg.backup.mac_root == "~/PhoneBackup"
    assert cfg.backup.device_label == "Phone"
    assert cfg.backup.ssd_volume_uuid == ""
    assert cfg.backup.required_destinations == ["mac", "ssd"]
    assert cfg.analysis.neardup_dhash_max == 8
    assert cfg.analysis.cluster_cosine == 0.88
    assert cfg.scoring.t_suggest == 0.97
    assert cfg.scoring.calibration_sample == 300
    assert cfg.cleanup.min_age_days == 7
    assert cfg.cleanup.max_delete_per_run == 5000
    assert cfg.slate.chunk_size == 500
    assert cfg.browse.typed_confirmation_over_files == 200
    assert cfg.apps.protected_packages == [
        "com.whatsapp",
        "com.whatsapp.w4b",
        "com.azure.authenticator",
        "com.google.android.apps.authenticator2",
        "com.authy.authy",
    ]
    assert cfg.health.storage_warn_pct == 90
    assert cfg.health.battery_temp_warn_c == 40


def test_never_delete_includes_amendments() -> None:
    nd = Config().cleanup.never_delete
    for p in [
        "/sdcard/Android/data",
        "/sdcard/Android/obb",
        "/sdcard/Documents",
        "/sdcard/Download",
        "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases",
        "/sdcard/Android/media/com.whatsapp/WhatsApp/Backups",
        "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Databases",
        "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Backups",
    ]:
        assert p in nd


def test_bad_value_falls_back_per_key(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[phone]\nadb_server_port = "abc"\nparallel_reads_usb = 4\n')
    cfg, w = load_config(f)
    assert cfg.phone.adb_server_port == 5037
    assert cfg.phone.parallel_reads_usb == 4
    assert any("phone.adb_server_port" in x for x in w)


def test_out_of_range_values_fall_back(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[backup]\nlayout = "sideways"\n[cleanup]\nmin_age_days = -1\n')
    cfg, w = load_config(f)
    assert cfg.backup.layout == "mirror"
    assert cfg.cleanup.min_age_days == 7
    assert len(w) == 2


def test_unknown_keys_warn(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("[phone]\nfoo = 1\n[bogus]\nx = 1\n")
    _, w = load_config(f)
    assert any("phone.foo" in x for x in w)
    assert any("bogus" in x for x in w)


def test_malformed_toml_is_set_aside(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("[phone\n")
    cfg, w = load_config(f)
    assert cfg == Config()
    assert not f.exists()
    assert len(list(tmp_path.glob("config.toml.invalid-*"))) == 1
    assert "could not be read" in w[0]


def test_default_sets() -> None:
    sets = Config().backup.sets
    assert [s.name for s in sets] == [
        "Camera",
        "Screenshots",
        "Pictures and Movies",
        "WhatsApp media",
        "WhatsApp Business",
        "Documents",
        "Downloads",
    ]
    by = {s.name: s for s in sets}
    assert by["Camera"].paths == ["/sdcard/DCIM"]
    assert by["Camera"].exclude == ["/sdcard/DCIM/Screenshots"]
    assert by["Documents"].enabled
    assert not by["Documents"].deletable
    assert not by["WhatsApp Business"].enabled
    assert not by["Downloads"].enabled
    assert all(s.adopt_mode == "hash" for s in sets)


def test_legacy_roots_become_one_custom_set(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[backup]\nroots = ["/sdcard/DCIM", "/sdcard/Music"]\n')
    cfg, w = load_config(f)
    assert [(s.name, s.paths, s.enabled, s.deletable) for s in cfg.backup.sets] == [
        ("Custom", ["/sdcard/DCIM", "/sdcard/Music"], True, False)
    ]
    assert any("backup.roots is replaced by backup.sets" in x for x in w)


def test_roots_ignored_when_sets_present(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[backup]\nroots = ["/sdcard/Music"]\n'
        '[[backup.sets]]\nname = "Camera"\npaths = ["/sdcard/DCIM"]\n'
        "enabled = true\ndeletable = true\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == ["Camera"]
    assert any("backup.roots" in x and "ignored" in x for x in w)


def test_invalid_set_entry_dropped_others_kept(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "Camera"\npaths = ["/sdcard/DCIM"]\nenabled = true\n'
        "deletable = true\n"
        '[[backup.sets]]\nname = "Bad"\npaths = []\nenabled = true\ndeletable = true\n'
        '[[backup.sets]]\nname = "Camera"\npaths = ["/sdcard/X"]\nenabled = true\n'
        "deletable = true\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == ["Camera"]
    assert cfg.backup.sets[0].paths == ["/sdcard/DCIM"]
    assert any('"Bad"' in x for x in w)
    assert any("duplicate" in x for x in w)


def _write_set(f: Path, body: str, name: str = "Camera") -> None:
    f.write_text(f'[[backup.sets]]\nname = "{name}"\n{body}enabled = true\ndeletable = true\n')


def test_set_exclude_outside_sdcard_is_dropped_set_kept(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    _write_set(f, 'paths = ["/sdcard/DCIM"]\nexclude = ["/tmp/x"]\n', name="A")
    cfg, w = load_config(f)
    assert [(s.name, s.exclude) for s in cfg.backup.sets] == [("A", [])]
    assert 'backup.sets[0] ("A").exclude: "/tmp/x" is not allowed (outside /sdcard); dropped' in w


FALLBACK_WARNING = (
    "no valid backup sets; using the default sets with deleting turned off "
    "until the settings are fixed"
)
DEFAULT_NAMES = [s.name for s in Config().backup.sets]


def test_no_valid_sets_falls_back_with_deleting_off(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[[backup.sets]]\nname = "Bad"\npaths = []\nenabled = true\ndeletable = true\n')
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == DEFAULT_NAMES
    assert all(not s.deletable for s in cfg.backup.sets)
    assert FALLBACK_WARNING in w
    assert any('"Bad"' in x for x in w)

    f.write_text("[backup]\nsets = []\n")
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == DEFAULT_NAMES
    assert all(not s.deletable for s in cfg.backup.sets)
    assert FALLBACK_WARNING in w

    f.write_text('[backup]\nsets = "oops"\n')
    cfg, w = load_config(f)
    assert all(not s.deletable for s in cfg.backup.sets)
    assert FALLBACK_WARNING in w


def test_fallback_does_not_mutate_default_sets(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("[backup]\nsets = []\n")
    load_config(f)
    by = {s.name: s for s in Config().backup.sets}
    assert by["Camera"].deletable
    assert not by["Documents"].deletable


def test_no_sets_key_keeps_spec_deletable_values(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[backup]\ndevice_label = "X"\n')
    cfg, w = load_config(f)
    by = {s.name: s for s in cfg.backup.sets}
    assert by["Camera"].deletable
    assert not by["Documents"].deletable
    assert FALLBACK_WARNING not in w
    cfg, w = load_config(tmp_path / "missing.toml")
    assert cfg.backup.sets[0].deletable
    assert w == []


def test_all_valid_sets_disabled_are_kept(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "A"\npaths = ["/sdcard/DCIM"]\nenabled = false\n'
        "deletable = true\n"
        '[[backup.sets]]\nname = "B"\npaths = ["/sdcard/Music"]\nenabled = false\n'
        "deletable = false\n"
    )
    cfg, w = load_config(f)
    assert [(s.name, s.enabled, s.deletable) for s in cfg.backup.sets] == [
        ("A", False, True),
        ("B", False, False),
    ]
    assert w == []


def test_unknown_key_in_set_warns_but_keeps_entry(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "Camera"\npaths = ["/sdcard/DCIM"]\nenabled = true\n'
        "deletable = true\ncolour = 3\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == ["Camera"]
    assert 'backup.sets[0] ("Camera").colour: unknown setting, ignored' in w


def test_fast_adopt_mode_warns(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[backup]\nadopt_mode = "fast"\n')
    cfg, w = load_config(f)
    assert cfg.backup.adopt_mode == "fast"
    assert any("fast is not built yet" in x for x in w)


def test_fast_adopt_mode_in_set_warns(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "Camera"\npaths = ["/sdcard/DCIM"]\nenabled = true\n'
        'deletable = true\nadopt_mode = "fast"\n'
    )
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].adopt_mode == "fast"
    assert any("fast is not built yet" in x for x in w)


def test_sets_roundtrip_as_array_of_tables(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    save_config(Config(), f)
    text = f.read_text()
    assert text.count("[[backup.sets]]") == 7
    # `delete_roots` is a real key; only the legacy `backup.roots` must be gone.
    assert not any(line.startswith("roots") for line in text.splitlines())
    assert load_config(f) == (Config(), [])


def test_save_roundtrip_keeps_comments(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("# my note\n[phone]\nkeep_awake = true\n")
    cfg, _ = load_config(f)
    cfg.phone.allow_delete_over_wifi = True
    save_config(cfg, f)
    assert "# my note" in f.read_text()
    again, w = load_config(f)
    assert w == []
    assert again.phone.keep_awake
    assert again.phone.allow_delete_over_wifi


def test_save_into_file_with_sets_keeps_keys_out_of_the_sets(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[backup]\nmac_root = "~/B"  # my disk\n'
        '[[backup.sets]]\nname = "Camera"\npaths = ["/sdcard/DCIM"]\n'
        "enabled = true\ndeletable = true\n"
    )
    cfg, _ = load_config(f)
    save_config(cfg, f)
    text = f.read_text()
    assert "# my disk" in text
    again, w = load_config(f)
    assert w == []
    assert again == cfg
    assert [s.name for s in again.backup.sets] == ["Camera"]


def test_storage_emulated_prefix_is_rewritten(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    _write_set(
        f,
        'paths = ["/storage/emulated/0/DCIM"]\n'
        'exclude = ["/storage/emulated/0/DCIM/Screenshots"]\n',
    )
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].paths == ["/sdcard/DCIM"]
    assert cfg.backup.sets[0].exclude == ["/sdcard/DCIM/Screenshots"]
    assert w == []


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("/data/local", "outside /sdcard"),
        ("/sdcardX/DCIM", "outside /sdcard"),
        ("/sdcard/", "no folder after /sdcard/"),
        ("/sdcard", "no folder after /sdcard/"),
        ("/storage/emulated/0/", "no folder after /sdcard/"),
        ("/sdcard/DCIM/../Documents", '".." segment'),
        ("/sdcard/./DCIM", '"." segment'),
        ("/sdcard/DCIM//Camera", "empty segment"),
        ("/sdcard/DCIM/", "trailing slash"),
    ],
)
def test_bad_path_entry_dropped_rest_of_set_kept(tmp_path: Path, bad: str, reason: str) -> None:
    f = tmp_path / "config.toml"
    _write_set(f, f'paths = ["{bad}", "/sdcard/Music"]\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].paths == ["/sdcard/Music"]
    assert f'backup.sets[0] ("Camera").paths: "{bad}" is not allowed ({reason}); dropped' in w
    assert len(w) == 1


def test_set_losing_all_paths_is_dropped(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "Bad"\npaths = ["/sdcard/../x", "/etc"]\n'
        "enabled = true\ndeletable = true\n"
        '[[backup.sets]]\nname = "Ok"\npaths = ["/sdcard/DCIM"]\nenabled = true\n'
        "deletable = true\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == ["Ok"]
    assert sum("backup.sets[0]" in x and ".paths:" in x for x in w) == 2
    assert any(x.startswith('backup.sets[0] ("Bad"):') and x.endswith("dropped") for x in w)
    assert FALLBACK_WARNING not in w

    f.write_text(
        '[[backup.sets]]\nname = "Bad"\npaths = ["/etc"]\nenabled = true\ndeletable = true\n'
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == DEFAULT_NAMES
    assert FALLBACK_WARNING in w


@pytest.mark.parametrize(
    "bad", ["/sdcard/Music", "/sdcard/DCIMX", "/sdcard/DCIMX/Cam", "/sdcard/DCIM"]
)
def test_exclude_must_lie_inside_the_sets_paths(tmp_path: Path, bad: str) -> None:
    f = tmp_path / "config.toml"
    _write_set(f, f'paths = ["/sdcard/DCIM"]\nexclude = ["{bad}", "/sdcard/DCIM/Screenshots"]\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].exclude == ["/sdcard/DCIM/Screenshots"]
    assert (
        f'backup.sets[0] ("Camera").exclude: "{bad}" is not inside any of the set\'s paths; dropped'
        in w
    )


def test_bad_exclude_entries_use_path_rules(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    _write_set(f, 'paths = ["/sdcard/DCIM"]\nexclude = ["/sdcard/DCIM/../x", "/sdcard/DCIM/a/"]\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].exclude == []
    assert any('".." segment' in x for x in w)
    assert any("trailing slash" in x for x in w)


def test_legacy_roots_follow_the_path_rules(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[backup]\nroots = ["/storage/emulated/0/DCIM", "/sdcard/..", "/sdcard/Music/"]\n')
    cfg, w = load_config(f)
    assert [(s.name, s.paths) for s in cfg.backup.sets] == [("Custom", ["/sdcard/DCIM"])]
    assert any('"/sdcard/.."' in x for x in w)
    assert any("trailing slash" in x for x in w)
    assert any("backup.roots is replaced by backup.sets" in x for x in w)

    f.write_text('[backup]\nroots = ["/etc"]\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets == Config().backup.sets  # spec defaults, not the fallback
    assert any("backup.roots" in x and "ignored" in x for x in w)


def test_model_rejects_bad_paths_directly() -> None:
    with pytest.raises(ValidationError):
        BackupSet(name="A", paths=["/sdcard/../x"], enabled=True, deletable=True)
    with pytest.raises(ValidationError):
        BackupSet(
            name="A",
            paths=["/sdcard/DCIM"],
            exclude=["/sdcard/DCIMX"],
            enabled=True,
            deletable=True,
        )
