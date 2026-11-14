import pytest

from sift.backup.sets import ScanRoot, claiming_roots, resolve_roots, set_for_path
from sift.core.config import DEFAULT_SETS, BackupSet
from sift.device.fake_phone import build_fake_phone


def _sets() -> list[BackupSet]:
    return [s.to_backup_set() for s in DEFAULT_SETS]


def _by_name(roots: list[ScanRoot]) -> dict[str, list[ScanRoot]]:
    out: dict[str, list[ScanRoot]] = {}
    for r in roots:
        out.setdefault(r.set_name, []).append(r)
    return out


def test_run_touches_only_ticked_sets() -> None:
    roots = resolve_roots(_sets(), ["Camera", "Documents"])
    assert {r.set_name for r in roots} == {"Camera", "Documents"}
    assert [r.path for r in roots] == ["/sdcard/DCIM", "/sdcard/Documents"]
    assert resolve_roots(_sets(), []) == []
    # A disabled set is still run when ticked; an enabled one is not when unticked.
    assert {r.set_name for r in resolve_roots(_sets(), ["Downloads"])} == {"Downloads"}


def test_ticked_names_match_ignoring_case_and_unknown_names_are_an_error() -> None:
    assert {r.set_name for r in resolve_roots(_sets(), ["camera"])} == {"Camera"}
    with pytest.raises(ValueError, match="Nope"):
        resolve_roots(_sets(), ["Camera", "Nope"])


def test_excluded_subfolder_not_scanned() -> None:
    (camera,) = _by_name(resolve_roots(_sets(), ["Camera"]))["Camera"]
    assert camera.excludes == ("/sdcard/DCIM/Screenshots",)
    roots = resolve_roots(_sets(), ["Camera"])
    assert set_for_path("/sdcard/DCIM/Camera/a.jpg", roots) == "Camera"
    assert set_for_path("/sdcard/DCIM/Screenshots/a.png", roots) is None
    assert set_for_path("/sdcard/dcim/screenshots/deeper/a.png", roots) is None
    # With Screenshots ticked as well, the excluded folder belongs to that set.
    both = resolve_roots(_sets(), ["Camera", "Screenshots"])
    assert set_for_path("/sdcard/DCIM/Screenshots/a.png", both) == "Screenshots"


def test_shallow_path_takes_direct_files_only() -> None:
    roots = resolve_roots(_sets(), ["Loose files"])
    (loose,) = roots
    assert loose.shallow and loose.path == "/sdcard"
    assert set_for_path("/sdcard/notes.txt", roots) == "Loose files"
    assert set_for_path("/sdcard/Download/a.bin", roots) is None
    assert set_for_path("/sdcard/a/b/c.bin", roots) is None
    assert set_for_path("/sdcard", roots) is None


def test_most_specific_root_wins_and_case_is_ignored() -> None:
    sets = [
        BackupSet(name="Wide", paths=["/sdcard/Pictures"], enabled=True, deletable=True),
        BackupSet(name="Narrow", paths=["/sdcard/Pictures/Trips"], enabled=True, deletable=True),
        BackupSet(name="Loose", shallow_paths=["/sdcard/Pictures"], enabled=True, deletable=True),
    ]
    roots = resolve_roots(sets, ["Wide", "Narrow", "Loose"])
    assert set_for_path("/sdcard/pictures/TRIPS/a.jpg", roots) == "Narrow"
    assert set_for_path("/sdcard/Pictures/Other/a.jpg", roots) == "Wide"
    assert set_for_path("/sdcard/Pictures/a.jpg", roots) == "Wide"  # tie: first listed wins
    assert set_for_path("/sdcard/PicturesX/a.jpg", roots) is None
    assert set_for_path("/storage/emulated/0/Pictures/Trips/a.jpg", roots) == "Narrow"


def test_loose_files_and_folder_sets_never_overlap() -> None:
    sets = _sets()
    roots = resolve_roots(sets, [s.name for s in sets])
    assert len(sets) == 10
    phone = build_fake_phone()
    checked = 0
    for path, f in phone.files.items():
        if f.is_symlink_to is not None:
            continue
        claims = claiming_roots(path, roots)
        assert len({r.set_name for r in claims}) <= 1, (path, claims)
        assert len(claims) <= 1, (path, claims)
        checked += 1
    assert checked > 100
    # And the sets actually cover the main folders.
    assert set_for_path("/sdcard/DCIM/Camera/a.jpg", roots) == "Camera"
    assert set_for_path("/sdcard/Pictures/Screenshots/a.png", roots) == "Screenshots"
    assert set_for_path("/sdcard/Pictures/a.jpg", roots) == "Pictures and Movies"
    assert set_for_path("/sdcard/WhatsApp/Media/WhatsApp Images/a.jpg", roots) == "WhatsApp media"
    assert (
        set_for_path(
            "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media/x/a.jpg", roots
        )
        == "WhatsApp Business"
    )
    assert set_for_path("/sdcard/notes.txt", roots) == "Loose files"
