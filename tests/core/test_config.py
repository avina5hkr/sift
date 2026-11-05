from pathlib import Path

from sift.core.config import Config, load_config, save_config


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


def test_set_exclude_must_start_with_sdcard(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(
        '[[backup.sets]]\nname = "A"\npaths = ["/sdcard/DCIM"]\nexclude = ["/tmp/x"]\n'
        "enabled = true\ndeletable = true\n"
        '[[backup.sets]]\nname = "B"\npaths = ["/sdcard/Music"]\nenabled = true\n'
        "deletable = true\n"
    )
    cfg, w = load_config(f)
    assert [s.name for s in cfg.backup.sets] == ["B"]
    assert any('backup.sets[0] ("A")' in x for x in w)


def test_no_valid_sets_uses_defaults(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('[[backup.sets]]\nname = "Bad"\npaths = []\nenabled = true\ndeletable = true\n')
    cfg, w = load_config(f)
    assert cfg.backup.sets == Config().backup.sets
    assert "no valid backup sets; using the default sets" in w
    assert any('"Bad"' in x for x in w)

    f.write_text("[backup]\nsets = []\n")
    cfg, w = load_config(f)
    assert cfg.backup.sets == Config().backup.sets
    assert "no valid backup sets; using the default sets" in w


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
