"""Phone paths in the database: valid UTF-8 unchanged, other bytes escaped and given back."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sift.backup.phonepaths import UnstorablePath, from_db, shown, storable, to_db
from sift.core.config import phone_path_key

NON_UTF8 = "/sdcard/Download/caf\udce9.jpg"  # one raw byte 0xE9


def test_a_valid_utf8_path_is_stored_unchanged() -> None:
    for path in ("/sdcard/DCIM/Camera/IMG_1.jpg", "/sdcard/Download/फोटो_१.jpg", "/a/%E9 233"):
        assert to_db(path) == path
        assert from_db(path) == path


def test_a_non_utf8_path_is_stored_as_valid_text_and_given_back() -> None:
    stored = to_db(NON_UTF8)
    stored.encode("utf-8")  # valid text for SQLite
    assert stored == "/sdcard/Download/caf\x00233.jpg"
    assert from_db(stored) == NON_UTF8
    assert from_db(stored).encode("utf-8", "surrogateescape") == b"/sdcard/Download/caf\xe9.jpg"


@given(st.lists(st.binary(min_size=1, max_size=12).filter(lambda b: b"\0" not in b), max_size=4))
def test_any_phone_bytes_round_trip(names: list[bytes]) -> None:
    raw = b"/sdcard/" + b"/".join(n.replace(b"/", b"_") for n in names)
    path = raw.decode("utf-8", "surrogateescape")
    stored = to_db(path)
    stored.encode("utf-8")
    assert from_db(stored) == path
    assert from_db(stored).encode("utf-8", "surrogateescape") == raw
    # Keys stored with to_db match the key of the path read back (the scan compares them).
    assert to_db(phone_path_key(path)) == phone_path_key(stored)
    assert from_db(phone_path_key(stored)) == phone_path_key(path)


def test_a_valid_path_that_looks_like_an_escaped_one_never_collides() -> None:
    lookalikes = ["/sdcard/Download/caf233.jpg", "/sdcard/Download/caf%E9.jpg", "/x/\\x00233"]
    stored = {to_db(p) for p in lookalikes}
    assert to_db(NON_UTF8) not in stored
    assert len(stored) == len(lookalikes)
    with pytest.raises(UnstorablePath):  # the escape's own character is never a phone name's
        to_db("/sdcard/Download/caf\x00233.jpg")


def test_paths_no_phone_gives_cannot_be_stored() -> None:
    assert not storable("/a/b\x00c")
    assert not storable("/a/\ud800")  # not a byte surrogateescape makes
    assert storable(NON_UTF8)


def test_shown_writes_each_odd_byte_as_percent_hex() -> None:
    assert shown(NON_UTF8) == "/sdcard/Download/caf%E9.jpg"
    assert shown("Could not copy /a/\ud800") == "Could not copy /a/�"
    assert shown("plain") == "plain"
