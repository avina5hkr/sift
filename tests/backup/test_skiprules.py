import pytest

from sift.backup.skiprules import SkipRules, is_skipped
from sift.core.config import BackupConfig, Config, load_config

WA = "/sdcard/Android/media/com.whatsapp/WhatsApp"
WAB = "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business"
LEGACY = "/sdcard/WhatsApp"
ROOTS = (f"{WA}/Media", f"{WAB}/Media", f"{LEGACY}/Media")


def rules(**over: bool) -> SkipRules:
    cfg = Config(backup=BackupConfig(**over))
    return SkipRules.from_config(cfg)


def skipped(
    path: str, *, size: int = 10, is_dir: bool = False, r: SkipRules | None = None
) -> str | None:
    return is_skipped(path, is_dir=is_dir, size=size, rules=r or rules())


def test_from_config_carries_the_editable_lists_and_toggles() -> None:
    r = rules(include_whatsapp_db=True, skip_zero_byte=False)
    assert ".thumbnails" in r.hidden_folders and ".Wamo" in r.hidden_folders
    assert r.name_prefixes == (".pending-", ".trashed-", ".tmp", ".nomedia")
    assert r.name_suffixes == (".tmp", ".crdownload", ".part")
    assert r.include_whatsapp_db and not r.include_whatsapp_statuses and not r.skip_zero_byte
    assert r.whatsapp_media_roots == ROOTS
    with pytest.raises(AttributeError):
        r.skip_zero_byte = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "name", [".thumbnails", ".trash", ".shared", ".cache", ".Links", ".wamocache", ".Wamo"]
)
def test_hidden_folder_names_are_skipped_anywhere(name: str) -> None:
    assert skipped(f"/sdcard/Pictures/{name}/a.jpg") == "hidden_folder"
    assert skipped(f"/sdcard/Pictures/{name}", is_dir=True, size=0) == "hidden_folder"
    assert skipped(f"/sdcard/Pictures/{name.upper()}/a.jpg") == "hidden_folder"
    assert skipped(f"/sdcard/Pictures/x/{name}/deep/a.jpg") == "hidden_folder"


@pytest.mark.parametrize("root", ROOTS)
@pytest.mark.parametrize("folder", [".c0ffee0123456789", ".random-folder", ".x", ".Links", ".Wamo"])
def test_every_whatsapp_dot_folder_is_skipped(root: str, folder: str) -> None:
    assert skipped(f"{root}/{folder}/IMG-1.jpg") is not None
    assert skipped(f"{root}/{folder}/sub/IMG-1.jpg") is not None
    assert skipped(f"{root}/{folder}", is_dir=True, size=0) is not None
    # even when the owner removed it from the editable list
    bare = SkipRules.from_config(Config(backup=BackupConfig(skip_hidden_folders=[])))
    assert skipped(f"{root}/.c0ffee0123456789/IMG-1.jpg", r=bare) == "whatsapp_hidden_folder"


@pytest.mark.parametrize("root", ROOTS)
def test_whatsapp_normal_folders_are_kept(root: str) -> None:
    assert skipped(f"{root}/WhatsApp Images/IMG-1.jpg") is None
    assert skipped(f"{root}/WhatsApp Images/Sent/IMG-1.jpg") is None
    assert skipped(f"{root}/ AI Media/IMG-1.jpg") is None
    assert skipped(f"{root}/WhatsApp Voice Notes/202401/PTT-1.opus") is None


def test_dot_folder_outside_a_whatsapp_root_is_not_skipped_unless_listed() -> None:
    assert skipped("/sdcard/Pictures/.private/a.jpg") is None
    assert skipped("/sdcard/Android/media/com.other/Media/.x/a.jpg") is None


def test_statuses_toggle() -> None:
    for root in ROOTS:
        path = f"{root}/.Statuses/VID-1.mp4"
        assert skipped(path) == "whatsapp_statuses"
        on = rules(include_whatsapp_statuses=True)
        assert skipped(path, r=on) is None
        assert skipped(f"{root}/.Statuses", is_dir=True, size=0, r=on) is None
        # other dot-folders stay skipped with the toggle on
        assert skipped(f"{root}/.Links/a.jpg", r=on) is not None
    on = rules(include_whatsapp_statuses=True)
    assert skipped("/sdcard/Pictures/.Statuses/a.jpg", r=on) == "hidden_folder"


@pytest.mark.parametrize("base", [WA, WAB, LEGACY])
@pytest.mark.parametrize("folder", ["Databases", "Backups"])
def test_whatsapp_databases_toggle(base: str, folder: str) -> None:
    path = f"{base}/{folder}/msgstore.db.crypt15"
    assert skipped(path) == "whatsapp_db"
    assert skipped(f"{base}/{folder}", is_dir=True, size=0) == "whatsapp_db"
    assert skipped(f"{base}/{folder.lower()}/sub/x.bin") == "whatsapp_db"
    assert skipped(path, r=rules(include_whatsapp_db=True)) is None
    # statuses setting does not open databases
    assert skipped(path, r=rules(include_whatsapp_statuses=True)) == "whatsapp_db"


def test_databases_folder_elsewhere_is_not_a_whatsapp_database() -> None:
    assert skipped("/sdcard/Documents/Databases/a.db") is None
    assert skipped("/sdcard/Android/media/com.other/WhatsApp/Databases/a.db") is None


@pytest.mark.parametrize(
    "name",
    [".pending-1700-IMG.jpg", ".trashed-1700-IMG.jpg", ".tmp123", ".tmpfile.jpg", ".nomedia"],
)
def test_name_prefixes(name: str) -> None:
    assert skipped(f"/sdcard/DCIM/Camera/{name}") == "name_prefix"
    assert skipped(f"/sdcard/DCIM/Camera/{name.upper()}") == "name_prefix"


@pytest.mark.parametrize("name", ["a.tmp", "movie.crdownload", "big.part", "A.PART"])
def test_name_suffixes(name: str) -> None:
    assert skipped(f"/sdcard/Download/{name}") == "name_suffix"


def test_ordinary_names_are_kept() -> None:
    for name in ("IMG_1.jpg", "tmp.jpg", "pending-x.jpg", "part.txt", "a.partial", "nomedia.jpg"):
        assert skipped(f"/sdcard/Download/{name}") is None


def test_zero_byte_files() -> None:
    assert skipped("/sdcard/Download/empty.dat", size=0) == "zero_byte"
    assert skipped("/sdcard/Download/empty.dat", size=1) is None
    assert skipped("/sdcard/Download/folder", is_dir=True, size=0) is None
    assert skipped("/sdcard/Download/empty.dat", size=0, r=rules(skip_zero_byte=False)) is None


def test_storage_spelling_and_case_are_ignored() -> None:
    assert skipped("/storage/emulated/0/Pictures/.thumbnails/a.jpg") == "hidden_folder"
    assert skipped(f"{WA.upper()}/DATABASES/a.db") == "whatsapp_db"


def test_config_defaults_and_bad_skip_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = Config()
    assert cfg.backup.skip_zero_byte is True
    path = tmp_path / "config.toml"
    path.write_text(
        '[backup]\nskip_hidden_folders = [".ok", "", "a/b", "  "]\n'
        'skip_name_prefixes = "x"\nskip_name_suffixes = [".ok", 3]\nskip_zero_byte = "yes"\n',
        encoding="utf-8",
    )
    loaded, warnings = load_config(path)
    assert loaded.backup.skip_hidden_folders == [".ok"]
    assert loaded.backup.skip_name_prefixes == list(Config().backup.skip_name_prefixes)
    assert loaded.backup.skip_name_suffixes == list(Config().backup.skip_name_suffixes)
    assert loaded.backup.skip_zero_byte is True
    text = "\n".join(warnings)
    assert 'backup.skip_hidden_folders: "" is blank; dropped' in text
    assert 'backup.skip_hidden_folders: "a/b" is a path' in text
    assert "backup.skip_name_prefixes: invalid value, using the default" in text
    assert "backup.skip_zero_byte: invalid value, using the default" in text


def test_skip_lists_survive_a_save_and_load(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sift.core.config import save_config

    cfg = Config(backup=BackupConfig(skip_hidden_folders=[".mine"], skip_zero_byte=False))
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    loaded, warnings = load_config(path)
    assert warnings == []
    assert loaded.backup.skip_hidden_folders == [".mine"]
    assert loaded.backup.skip_zero_byte is False
