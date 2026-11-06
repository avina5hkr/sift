from pathlib import Path

import pytest
from pydantic import ValidationError

from sift.core.config import DEFAULT_SETS, BackupSet, Config, load_config, save_config


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


def test_malformed_toml_is_set_aside_and_deleting_turned_off(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("[phone\n")
    cfg, w = load_config(f)
    assert len(list(tmp_path.glob("config.toml.invalid-*"))) == 1
    assert "could not be read" in w[0]
    assert FALLBACK_WARNING in w
    assert [s.name for s in cfg.backup.sets] == DEFAULT_NAMES
    assert all(not s.deletable for s in cfg.backup.sets)
    # a fresh config.toml holds the safe state, so the next launch keeps it
    assert f.exists()
    again, w2 = load_config(f)
    assert w2 == []
    assert [s.name for s in again.backup.sets] == DEFAULT_NAMES
    assert all(not s.deletable for s in again.backup.sets)


def test_malformed_toml_survives_when_the_fresh_file_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sift.core.config as config_mod

    def boom(cfg: Config, path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_mod, "save_config", boom)
    f = tmp_path / "config.toml"
    f.write_text("[phone\n")
    cfg, w = load_config(f)
    assert FALLBACK_WARNING in w
    assert any("disk full" in x for x in w)
    assert all(not s.deletable for s in cfg.backup.sets)


def test_non_table_backup_uses_fallback_sets_without_rewriting(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("backup = 3\n")
    cfg, w = load_config(f)
    assert FALLBACK_WARNING in w
    assert any(x.startswith("backup:") for x in w)
    assert all(not s.deletable for s in cfg.backup.sets)
    assert f.read_text() == "backup = 3\n"


def test_unreadable_config_never_raises(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.mkdir()
    cfg, w = load_config(f)
    assert len(w) == 2
    assert w[0].startswith("config.toml could not be opened (")
    assert w[0].endswith("); defaults are in use with deleting turned off.")
    assert FALLBACK_WARNING in w
    assert all(not s.deletable for s in cfg.backup.sets)
    assert f.is_dir()


def test_missing_file_keeps_spec_deletable_defaults(tmp_path: Path) -> None:
    cfg, w = load_config(tmp_path / "config.toml")
    by = {s.name: s for s in cfg.backup.sets}
    assert w == []
    assert by["Camera"].deletable
    assert not by["Documents"].deletable


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
        "Recordings",
        "Loose files",
    ]
    by = {s.name: s for s in sets}
    assert by["Camera"].paths == ["/sdcard/DCIM"]
    assert by["Camera"].exclude == ["/sdcard/DCIM/Screenshots"]
    assert by["Documents"].enabled
    assert not by["Documents"].deletable
    assert not by["Downloads"].enabled
    assert by["WhatsApp media"].paths == [
        "/sdcard/Android/media/com.whatsapp/WhatsApp/Media",
        "/sdcard/WhatsApp/Media",
    ]
    assert by["WhatsApp Business"].enabled
    assert by["WhatsApp Business"].deletable
    assert by["WhatsApp Business"].paths == [
        "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media"
    ]
    rec = by["Recordings"]
    assert (rec.paths, rec.shallow_paths, rec.enabled, rec.deletable) == (
        ["/sdcard/Recordings"],
        [],
        True,
        True,
    )
    loose = by["Loose files"]
    assert (loose.paths, loose.shallow_paths, loose.exclude) == ([], ["/sdcard"], [])
    assert loose.enabled
    assert not loose.deletable
    assert all(s.adopt_mode == "hash" for s in sets)


def test_never_delete_includes_old_whatsapp_folders() -> None:
    nd = Config().cleanup.never_delete
    assert "/sdcard/WhatsApp/Databases" in nd
    assert "/sdcard/WhatsApp/Backups" in nd
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
    assert text.count("[[backup.sets]]") == 9
    assert text.count("shallow_paths") == 9
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


@pytest.mark.parametrize("roots", ['["/etc"]', "[]", '"/sdcard/DCIM"', '["/sdcard/", "/x"]'])
def test_invalid_legacy_roots_use_the_safe_fallback(tmp_path: Path, roots: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(f"[backup]\nroots = {roots}\n")
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == DEFAULT_NAMES
    assert all(not s.deletable for s in cfg.backup.sets)
    assert FALLBACK_WARNING in w
    assert any(x.startswith("backup.roots") for x in w)


def test_legacy_roots_with_one_bad_entry_keep_the_good_one(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[backup]\nroots = ["/sdcard/Music", "/etc"]\n')
    cfg, w = load_config(f)
    assert [(s.name, s.paths, s.deletable) for s in cfg.backup.sets] == [
        ("Custom", ["/sdcard/Music"], False)
    ]
    assert 'backup.roots: "/etc" is not allowed (outside /sdcard); dropped' in w
    assert FALLBACK_WARNING not in w


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


def test_shallow_root_allowed_only_in_shallow_paths(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    _write_set(
        f,
        'paths = ["/sdcard/DCIM", "/sdcard"]\nexclude = ["/sdcard"]\nshallow_paths = ["/sdcard"]\n',
    )
    cfg, w = load_config(f)
    s = cfg.backup.sets[0]
    assert (s.paths, s.exclude, s.shallow_paths) == (["/sdcard/DCIM"], [], ["/sdcard"])
    assert (
        'backup.sets[0] ("Camera").paths: "/sdcard" is not allowed (no folder after /sdcard/); '
        "dropped"
    ) in w
    assert 'backup.sets[0] ("Camera").exclude: "/sdcard" is not allowed' in " ".join(w)
    assert len(w) == 2

    _write_set(f, 'paths = []\nshallow_paths = ["/storage/emulated/0"]\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].shallow_paths == ["/sdcard"]
    assert w == []

    with pytest.raises(ValidationError):
        BackupSet(name="A", paths=["/sdcard"], enabled=True, deletable=True)
    ok = BackupSet(name="A", shallow_paths=["/storage/emulated/0"], enabled=True, deletable=True)
    assert ok.shallow_paths == ["/sdcard"]


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("/data/local", "outside /sdcard"),
        ("/sdcard/DCIM/../Documents", '".." segment'),
        ("/sdcard/./DCIM", '"." segment'),
        ("/sdcard/DCIM//Camera", "empty segment"),
        ("/sdcard/DCIM/", "trailing slash"),
        ("/sdcard/", "trailing slash"),
    ],
)
def test_shallow_paths_follow_path_rules(tmp_path: Path, bad: str, reason: str) -> None:
    f = tmp_path / "config.toml"
    _write_set(f, f'paths = ["/sdcard/Music"]\nshallow_paths = ["{bad}", "/sdcard/Notes"]\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].paths == ["/sdcard/Music"]
    assert cfg.backup.sets[0].shallow_paths == ["/sdcard/Notes"]
    where = 'backup.sets[0] ("Camera").shallow_paths'
    expected = f'{where}: "{bad}" is not allowed ({reason}); dropped'
    assert expected in w
    assert len(w) == 1


def test_set_needs_paths_or_shallow_paths(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "Empty"\npaths = []\nshallow_paths = []\n'
        "enabled = true\ndeletable = true\n"
        '[[backup.sets]]\nname = "Flat"\npaths = []\nshallow_paths = ["/sdcard"]\n'
        "enabled = true\ndeletable = false\n"
        '[[backup.sets]]\nname = "Flat2"\nshallow_paths = ["/sdcard/Notes"]\n'
        "enabled = true\ndeletable = false\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == ["Flat", "Flat2"]
    assert (
        'backup.sets[0] ("Empty"): needs at least one entry in paths or shallow_paths; dropped'
    ) in w
    assert FALLBACK_WARNING not in w

    # every shallow entry invalid and no paths: the set is dropped, then the fallback applies
    f.write_text(
        '[[backup.sets]]\nname = "Bad"\npaths = []\nshallow_paths = ["/etc"]\n'
        "enabled = true\ndeletable = true\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == DEFAULT_NAMES
    assert len(cfg.backup.sets) == 9
    assert FALLBACK_WARNING in w
    assert any('("Bad").shallow_paths: "/etc"' in x for x in w)

    with pytest.raises(ValidationError):
        BackupSet(name="A", paths=[], shallow_paths=[], enabled=True, deletable=True)


def test_exclude_cannot_target_shallow_paths(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    _write_set(
        f,
        'paths = ["/sdcard/DCIM"]\nshallow_paths = ["/sdcard/Notes"]\n'
        'exclude = ["/sdcard/Notes/old", "/sdcard/DCIM/Screenshots"]\n',
    )
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].exclude == ["/sdcard/DCIM/Screenshots"]
    assert (
        'backup.sets[0] ("Camera").exclude: "/sdcard/Notes/old" is not inside any of the '
        "set's paths; dropped"
    ) in w

    with pytest.raises(ValidationError):
        BackupSet(
            name="A",
            paths=["/sdcard/DCIM"],
            shallow_paths=["/sdcard/Notes"],
            exclude=["/sdcard/Notes/old"],
            enabled=True,
            deletable=True,
        )


def test_fallback_has_nine_sets_none_deletable(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("[backup]\nsets = []\n")
    cfg, w = load_config(f)
    assert len(cfg.backup.sets) == 9
    assert all(not s.deletable for s in cfg.backup.sets)
    assert FALLBACK_WARNING in w


def test_bare_storage_root_in_paths_is_dropped(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    _write_set(f, 'paths = ["/storage/emulated/0", "/sdcard/Music"]\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets[0].paths == ["/sdcard/Music"]
    assert (
        'backup.sets[0] ("Camera").paths: "/storage/emulated/0" is not allowed '
        "(no folder after /sdcard/); dropped"
    ) in w


def test_set_warnings_are_plain_words(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "NoPaths"\npaths = []\nenabled = true\ndeletable = true\n'
        '[[backup.sets]]\nname = " "\npaths = ["/sdcard/A"]\nenabled = true\ndeletable = true\n'
        '[[backup.sets]]\nname = 3\npaths = "x"\nenabled = "yes"\ndeletable = 1\n'
        '[[backup.sets]]\nname = "L"\npaths = ["/sdcard/A"]\nshallow_paths = "x"\n'
        'exclude = 5\nenabled = true\ndeletable = true\nadopt_mode = "zzz"\n'
        '[[backup.sets]]\nname = "E"\npaths = [3]\nshallow_paths = [4]\nenabled = true\n'
        "deletable = true\n"
        '[[backup.sets]]\nname = "Nodel"\npaths = ["/sdcard/A"]\nenabled = true\n'
    )
    _, w = load_config(f)
    assert len(w) >= 5
    for x in w:
        assert "Value error" not in x
        assert "Input should be" not in x
    assert any(x.endswith("name: name must not be blank; dropped") for x in w)
    assert any("paths: must be a list" in x for x in w)
    assert any("name: must be text" in x for x in w)
    assert any("deletable: is required" in x for x in w)


FLOOR = [
    "/sdcard/Android/data",
    "/sdcard/Android/obb",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Backups",
    "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Databases",
    "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Backups",
    "/sdcard/WhatsApp/Databases",
    "/sdcard/WhatsApp/Backups",
]


def test_user_never_delete_cannot_drop_builtin_protection(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[cleanup]\nnever_delete = ["/sdcard/Mine"]\n[browse]\n'
        'never_delete_extra = ["/sdcard/Extra"]\n'
    )
    cfg, _ = load_config(f)
    eff = cfg.effective_never_delete()
    for p in FLOOR:
        assert p in eff
    assert "/sdcard/Mine" in eff
    assert "/sdcard/Extra" in eff
    assert len(eff) == len(set(eff))
    # the floor holds for an in-memory config too
    bare = Config()
    bare.cleanup.never_delete = []
    assert set(FLOOR) <= set(bare.effective_never_delete())


def test_default_never_delete_gives_no_floor_warning(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    save_config(Config(), f)
    assert load_config(f)[1] == []


def test_never_delete_missing_floor_entry_warns(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[cleanup]\nnever_delete = ["/sdcard/Mine", "/sdcard/android/DATA"]\n')
    _, w = load_config(f)
    missing = [x for x in w if "never_delete" in x and "protected" in x]
    assert len(missing) == len(FLOOR) - 1  # the case-differing Android/data counts as present
    assert not any("Android/data" in x for x in missing)
    assert any("/sdcard/Android/obb" in x for x in missing)


def test_path_lists_are_normalised_and_bad_entries_dropped(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[cleanup]\ndelete_roots = ["/storage/emulated/0/DCIM", "/sdcard/a/../b", "/tmp/x"]\n'
        'never_delete = ["/sdcard/Keep/", "/sdcard/Keep2"]\n'
        '[slate]\nkeep_folders = ["/storage/emulated/0/Documents", "/sdcard//x", "rel"]\n'
        '[browse]\nnever_delete_extra = ["/sdcard", "/sdcard/Ok"]\n'
    )
    cfg, w = load_config(f)
    assert cfg.cleanup.delete_roots == ["/sdcard/DCIM"]
    assert cfg.cleanup.never_delete == ["/sdcard/Keep2"]
    assert cfg.slate.keep_folders == ["/sdcard/Documents"]
    assert cfg.browse.never_delete_extra == ["/sdcard/Ok"]
    for where, item in [
        ("cleanup.delete_roots", "/sdcard/a/../b"),
        ("cleanup.delete_roots", "/tmp/x"),
        ("cleanup.never_delete", "/sdcard/Keep/"),
        ("slate.keep_folders", "/sdcard//x"),
        ("slate.keep_folders", "rel"),
        ("browse.never_delete_extra", "/sdcard"),
    ]:
        assert any(x.startswith(where) and f'"{item}"' in x and "dropped" in x for x in w), where


def test_path_lists_reject_bad_entries_in_code() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"slate": {"keep_folders": ["/nope"]}})
    assert Config.model_validate(
        {"browse": {"never_delete_extra": ["/storage/emulated/0/X"]}}
    ).browse.never_delete_extra == ["/sdcard/X"]


@pytest.mark.parametrize("bad", ["PhoneBackup", "", "relative/dir", "./x"])
def test_relative_mac_root_falls_back(tmp_path: Path, bad: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(f'[backup]\nmac_root = "{bad}"\n')
    cfg, w = load_config(f)
    assert cfg.backup.mac_root == "~/PhoneBackup"
    assert any(x.startswith("backup.mac_root") and "~/PhoneBackup" in x for x in w)
    with pytest.raises(ValidationError):
        Config.model_validate({"backup": {"mac_root": bad}})


@pytest.mark.parametrize("good", ["/Volumes/Disk/Backup", "~/B", "~"])
def test_absolute_mac_root_is_kept(tmp_path: Path, good: str) -> None:
    f = tmp_path / "config.toml"
    f.write_text(f'[backup]\nmac_root = "{good}"\n')
    cfg, w = load_config(f)
    assert cfg.backup.mac_root == good
    assert w == []


def test_save_config_replaces_a_non_table_section(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("phone = 3\n")
    save_config(Config(), f)
    cfg, w = load_config(f)
    assert w == []
    assert cfg == Config()


def test_save_config_replaces_a_non_table_backup_and_sets(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('backup = "x"\n')
    save_config(Config(), f)
    assert load_config(f) == (Config(), [])
    f.write_text("[backup]\nsets = 3\n")
    save_config(Config(), f)
    assert load_config(f) == (Config(), [])


def test_two_set_asides_in_the_same_second_keep_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    f = tmp_path / "config.toml"
    for _ in range(2):
        f.write_text("[phone\n")
        load_config(f)
    assert len(list(tmp_path.glob("config.toml.invalid-*"))) == 2


def test_default_sets_cannot_be_mutated() -> None:
    assert isinstance(DEFAULT_SETS, tuple)
    assert len(DEFAULT_SETS) == 9
    first = DEFAULT_SETS[0]
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        first.name = "x"  # type: ignore[misc]
    with pytest.raises((TypeError, AttributeError)):
        first.paths.append("/sdcard/X")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        DEFAULT_SETS[0] = first  # type: ignore[index]


def test_config_sets_are_independent_copies() -> None:
    a = Config()
    a.backup.sets[0].paths.append("/sdcard/Extra")
    a.backup.sets[0].name = "Changed"
    assert Config().backup.sets[0].paths == ["/sdcard/DCIM"]
    assert Config().backup.sets[0].name == "Camera"
    assert DEFAULT_SETS[0].name == "Camera"


def test_set_names_are_trimmed_and_case_folded_for_uniqueness(tmp_path: Path) -> None:
    sets = [
        BackupSet(name="Camera", paths=["/sdcard/DCIM"], enabled=True, deletable=True),
        BackupSet(name=" camera ", paths=["/sdcard/X"], enabled=True, deletable=True),
    ]
    assert sets[1].name == "camera"
    with pytest.raises(ValidationError, match="unique"):
        Config.model_validate({"backup": {"sets": sets}})
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "Camera"\npaths = ["/sdcard/DCIM"]\nenabled = true\n'
        "deletable = true\n"
        '[[backup.sets]]\nname = " camera "\npaths = ["/sdcard/X"]\nenabled = true\n'
        "deletable = true\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == ["Camera"]
    assert any("duplicate" in x for x in w)


def test_exclude_is_matched_case_insensitively(tmp_path: Path) -> None:
    s = BackupSet(
        name="Camera",
        paths=["/sdcard/DCIM"],
        exclude=["/sdcard/dcim/Screenshots"],
        enabled=True,
        deletable=True,
    )
    assert s.exclude == ["/sdcard/dcim/Screenshots"]
    f = tmp_path / "config.toml"
    _write_set(f, 'paths = ["/sdcard/DCIM"]\nexclude = ["/SDCARD/x"]\n')
    _, w = load_config(f)
    assert any("outside /sdcard" in x for x in w)
    _write_set(f, 'paths = ["/sdcard/DCIM"]\nexclude = ["/sdcard/dcim/Thumbs"]\n')
    cfg, w = load_config(f)
    assert w == []
    assert cfg.backup.sets[0].exclude == ["/sdcard/dcim/Thumbs"]
    with pytest.raises(ValidationError):
        BackupSet(
            name="C",
            paths=["/sdcard/DCIM"],
            exclude=["/sdcard/DCIMX"],
            enabled=True,
            deletable=True,
        )
