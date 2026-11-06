"""File-name rules and backup layout: pure functions, no database and no phone."""

import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from sift.backup.layout import (
    by_date_rel_path,
    exif_taken,
    mirror_rel_path,
    rel_key,
    resolve_collision,
    sanitize_component,
)

H8 = "ab12cd34"


def san(name: str) -> str:
    return sanitize_component(name, hash8=H8)


# --- sanitize_component -------------------------------------------------------------------


@pytest.mark.parametrize("char", list('<>:"/\\|?*'))
def test_forbidden_characters_become_underscores(char: str) -> None:
    assert san(f"a{char}b.txt") == "a_b.txt"


def test_control_characters_become_underscores() -> None:
    assert san("a\x00b\x1fc\x7fd\x85e.txt") == "a_b_c_d_e.txt"


def test_trailing_dots_and_spaces_are_stripped() -> None:
    assert san("Sent.") == "Sent"
    assert san("Sent...") == "Sent"
    assert san("name . . ") == "name"


def test_leading_space_is_kept() -> None:
    assert san(" AI Media") == " AI Media"


@pytest.mark.parametrize(
    "name", ["WhatsApp Backup Excluded Stickers", "WhatsApp AI Media", "WhatsApp Video Notes"]
)
def test_real_folder_names_are_unchanged(name: str) -> None:
    assert san(name) == name


def test_dot_only_names_cannot_climb_or_vanish() -> None:
    assert san("..") == "_"
    assert san(".") == "_"
    assert san("") == "_"
    assert san(" ") == "_"


def test_leading_dot_is_kept() -> None:
    assert san(".nomedia") == ".nomedia"


def test_name_is_nfc() -> None:
    decomposed = "Café.jpg"
    out = san(decomposed)
    assert out == "Café.jpg"
    assert unicodedata.is_normalized("NFC", out)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CON", f"CON_{H8}"),
        ("con.txt", f"con_{H8}.txt"),
        ("Nul.tar.gz", f"Nul_{H8}.tar.gz"),
        ("COM1", f"COM1_{H8}"),
        ("lpt9.jpg", f"lpt9_{H8}.jpg"),
        ("AUX .txt", f"AUX_{H8}.txt"),
    ],
)
def test_reserved_names_get_the_hash(name: str, expected: str) -> None:
    assert san(name) == expected


def test_names_that_only_start_like_reserved_ones_are_unchanged() -> None:
    assert san("CONSOLE.txt") == "CONSOLE.txt"
    assert san("COM10") == "COM10"


def test_long_name_keeps_extension_and_hash_within_255_bytes() -> None:
    out = san("a" * 400 + ".jpg")
    assert len(out.encode()) <= 255
    assert out.endswith(f"_{H8}.jpg")
    assert out.startswith("a" * 200)


def test_long_multibyte_name_is_cut_on_a_character_boundary() -> None:
    out = san("é" * 300 + ".mp4")
    assert len(out.encode()) <= 255
    assert out.endswith(f"_{H8}.mp4")
    assert unicodedata.is_normalized("NFC", out)
    out.encode()  # valid UTF-8, no lone surrogates


def test_name_of_exactly_255_bytes_is_untouched() -> None:
    name = "a" * 251 + ".jpg"
    assert len(name.encode()) == 255
    assert san(name) == name


def test_long_name_with_absurd_extension_still_fits() -> None:
    out = san("a." + "b" * 400)
    assert len(out.encode()) <= 255
    assert out.endswith(f"_{H8}")


def test_non_utf8_name_maps_to_valid_stable_utf8() -> None:
    raw = b"IMG_\xff\xfe_\xe9.jpg".decode("utf-8", errors="surrogateescape")
    out = san(raw)
    assert out == "IMG_%FF%FE_%E9.jpg"
    out.encode("utf-8")
    assert san(raw) == out


def test_other_lone_surrogates_become_replacement_character() -> None:
    assert san("a\ud800b") == "a�b"


def test_different_invalid_bytes_stay_distinct() -> None:
    a = b"x\xff".decode("utf-8", errors="surrogateescape")
    b = b"x\xfe".decode("utf-8", errors="surrogateescape")
    assert san(a) != san(b)


# --- rel_key ------------------------------------------------------------------------------


def test_rel_key_folds_case_and_normalises() -> None:
    assert rel_key("Pixel/DCIM/IMG.JPG") == rel_key("pixel/dcim/img.jpg")
    assert rel_key("Café/a") == rel_key("Café/a")
    assert unicodedata.is_normalized("NFC", rel_key("Straße/ẛ̣"))


# --- mirror / by_date ---------------------------------------------------------------------


def test_mirror_strips_sdcard_prefix() -> None:
    out = mirror_rel_path("Pixel", "/sdcard/DCIM/Camera/IMG_1.jpg", hash8=H8)
    assert out == "Pixel/DCIM/Camera/IMG_1.jpg"


def test_mirror_strips_emulated_prefix() -> None:
    out = mirror_rel_path("Pixel", "/storage/emulated/0/Download/a.pdf", hash8=H8)
    assert out == "Pixel/Download/a.pdf"


def test_mirror_sanitises_every_component_and_the_label() -> None:
    out = mirror_rel_path("My: Phone.", "/sdcard/ AI Media/Documents/Sent./a?.txt", hash8=H8)
    assert out == "My_ Phone/ AI Media/Documents/Sent/a_.txt"


def test_mirror_never_climbs_out() -> None:
    out = mirror_rel_path("Pixel", "/sdcard/../../etc/./passwd", hash8=H8)
    assert out == "Pixel/_/_/etc/passwd"


def test_mirror_long_folder_name_does_not_depend_on_the_file() -> None:
    folder = "f" * 300
    a = mirror_rel_path("P", f"/sdcard/{folder}/one.jpg", hash8="11111111")
    b = mirror_rel_path("P", f"/sdcard/{folder}/two.jpg", hash8="22222222")
    assert a.split("/")[:2] == b.split("/")[:2]
    assert all(len(part.encode()) <= 255 for part in a.split("/"))


def test_mirror_non_utf8_path_is_valid_utf8() -> None:
    raw = b"/sdcard/Download/\xc3(.txt".decode("utf-8", errors="surrogateescape")
    out = mirror_rel_path("Pixel", raw, hash8=H8)
    out.encode("utf-8")
    assert out == "Pixel/Download/%C3(.txt"


def test_by_date_layout() -> None:
    out = by_date_rel_path(
        "Pixel", "/sdcard/DCIM/Camera/IMG_1.jpg", taken=datetime(2024, 3, 9, 12, 0), hash8=H8
    )
    assert out == "Pixel/Camera/2024/2024-03/IMG_1.jpg"


def test_by_date_sanitises_the_name() -> None:
    out = by_date_rel_path(
        "Pixel", "/sdcard/DCIM/Camera/a:b.", taken=datetime(2024, 12, 31), hash8=H8
    )
    assert out == "Pixel/Camera/2024/2024-12/a_b"


# --- resolve_collision --------------------------------------------------------------------


def owners(table: dict[str, str]) -> Callable[[str], str | None]:
    return lambda key: table.get(key)


def test_free_path_is_kept() -> None:
    out = resolve_collision("P/a.jpg", hash8=H8, owner_of=owners({}), content_hash=H8 + "0" * 56)
    assert out == "P/a.jpg"


def test_same_content_keeps_the_path() -> None:
    content = H8 + "0" * 56
    out = resolve_collision(
        "P/a.jpg", hash8=H8, owner_of=owners({rel_key("P/a.jpg"): content}), content_hash=content
    )
    assert out == "P/a.jpg"


def test_different_content_gets_the_hash_before_the_extension() -> None:
    out = resolve_collision(
        "P/dir/a.jpg",
        hash8=H8,
        owner_of=owners({rel_key("P/dir/a.jpg"): "f" * 64}),
        content_hash=H8 + "0" * 56,
    )
    assert out == f"P/dir/a_{H8}.jpg"


def test_files_differing_only_by_case_get_distinct_paths() -> None:
    first = "1" * 64
    second = "2" * 64
    table: dict[str, str] = {}
    p1 = resolve_collision(
        "P/IMG.jpg", hash8="11111111", owner_of=owners(table), content_hash=first
    )
    table[rel_key(p1)] = first
    p2 = resolve_collision(
        "P/img.jpg", hash8="22222222", owner_of=owners(table), content_hash=second
    )
    assert p1 == "P/IMG.jpg"
    assert p2 == "P/img_22222222.jpg"
    assert rel_key(p1) != rel_key(p2)


def test_hash_suffixed_path_already_used_by_other_content_is_not_reused() -> None:
    content = H8 + "0" * 56
    table = {rel_key("P/a.jpg"): "f" * 64, rel_key(f"P/a_{H8}.jpg"): "e" * 64}
    out = resolve_collision("P/a.jpg", hash8=H8, owner_of=owners(table), content_hash=content)
    assert out not in ("P/a.jpg", f"P/a_{H8}.jpg")
    assert rel_key(out) not in table


def test_resumed_file_finds_its_own_hash_path() -> None:
    content = H8 + "0" * 56
    table = {rel_key("P/a.jpg"): "f" * 64, rel_key(f"P/a_{H8}.jpg"): content}
    out = resolve_collision("P/a.jpg", hash8=H8, owner_of=owners(table), content_hash=content)
    assert out == f"P/a_{H8}.jpg"


def test_collision_suffix_respects_the_255_byte_limit() -> None:
    name = "a" * 251 + ".jpg"
    path = f"P/{name}"
    out = resolve_collision(
        path, hash8=H8, owner_of=owners({rel_key(path): "f" * 64}), content_hash=H8 + "0" * 56
    )
    last = out.rsplit("/", 1)[1]
    assert len(last.encode()) <= 255
    assert last.endswith(f"_{H8}.jpg")


def test_collision_on_extensionless_name() -> None:
    out = resolve_collision(
        "P/README",
        hash8=H8,
        owner_of=owners({rel_key("P/README"): "f" * 64}),
        content_hash=H8 + "0" * 56,
    )
    assert out == f"P/README_{H8}"


# --- exif_taken ---------------------------------------------------------------------------

EXIF_IFD = 0x8769
DATE_TIME_ORIGINAL = 0x9003
DATE_TIME = 0x0132


def make_jpeg(path: Path, *, original: str | None = None, plain_date: str | None = None) -> None:
    img = Image.new("RGB", (4, 4), (10, 20, 30))
    exif = Image.Exif()
    if original is not None:
        exif.get_ifd(EXIF_IFD)[DATE_TIME_ORIGINAL] = original
    if plain_date is not None:
        exif[DATE_TIME] = plain_date
    if original is None and plain_date is None:
        img.save(path, "JPEG")
    else:
        img.save(path, "JPEG", exif=exif)


def test_exif_taken_reads_date_time_original(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    make_jpeg(p, original="2023:07:14 09:30:05")
    assert exif_taken(p) == datetime(2023, 7, 14, 9, 30, 5)


def test_exif_taken_without_exif_is_none(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    make_jpeg(p)
    assert exif_taken(p) is None


def test_exif_taken_ignores_the_modify_date(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    make_jpeg(p, plain_date="2023:07:14 09:30:05")
    assert exif_taken(p) is None


@pytest.mark.parametrize("value", ["0000:00:00 00:00:00", "not a date", "2023:13:40 99:99:99", ""])
def test_exif_taken_unparseable_date_is_none(tmp_path: Path, value: str) -> None:
    p = tmp_path / "a.jpg"
    make_jpeg(p, original=value)
    assert exif_taken(p) is None


def test_exif_taken_on_garbage_is_none(tmp_path: Path) -> None:
    p = tmp_path / "junk.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe1 this is not a jpeg" * 10)
    assert exif_taken(p) is None


def test_exif_taken_on_missing_or_empty_file_is_none(tmp_path: Path) -> None:
    assert exif_taken(tmp_path / "missing.jpg") is None
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    assert exif_taken(empty) is None
    assert exif_taken(tmp_path) is None
