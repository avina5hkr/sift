"""Shell command builders and output parsers for the real phone transport.

The fixtures in `fixtures/` are synthetic: output in the shape Android 11+ prints it (toybox
`df`, `wm size`, `dumpsys battery`) with made-up values. Nothing comes from a real phone.
"""

import shlex
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sift.device.shell import (
    build_sha256_script,
    encode_command,
    parse_battery,
    parse_df,
    parse_sha256_output,
    parse_wm_size,
    tail_command,
)
from sift.device.transport import RemoteError

FIXTURES = Path(__file__).parent / "fixtures"
HASH_A = "a" * 64
HASH_B = "0123456789abcdef" * 4


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# -- df -----------------------------------------------------------------------------------------


def test_parse_df_reads_free_and_total_bytes() -> None:
    assert parse_df(fixture("df_sdcard.txt")) == (132115952 * 1024, 231011768 * 1024)


def test_parse_df_handles_a_wrapped_filesystem_name() -> None:
    assert parse_df(fixture("df_sdcard_wrapped.txt")) == (132115952 * 1024, 231011768 * 1024)


@pytest.mark.parametrize(
    "out",
    [
        "",
        "Filesystem      1K-blocks     Used Available Use% Mounted on\n",
        "df: /sdcard: Permission denied\n",
        "Filesystem Size Used Avail Use% Mounted on\n/dev/fuse 220G 94G 126G 43% /storage\n",
        "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/fuse lots some few 4% /x\n",
    ],
)
def test_parse_df_gives_none_for_output_it_does_not_understand(out: str) -> None:
    assert parse_df(out) is None


def test_parse_df_fixture_for_errors() -> None:
    assert parse_df(fixture("df_permission_denied.txt")) is None


# -- wm size ------------------------------------------------------------------------------------


def test_parse_wm_size_physical_only() -> None:
    assert parse_wm_size(fixture("wm_size.txt")) == ((1080, 2400),)


def test_parse_wm_size_physical_and_override() -> None:
    assert parse_wm_size(fixture("wm_size_override.txt")) == ((1080, 2400), (720, 1600))


@pytest.mark.parametrize("out", ["", "Error: no display\n", "Physical size: 0x2400\n"])
def test_parse_wm_size_gives_nothing_for_unusable_output(out: str) -> None:
    assert parse_wm_size(out) == ()


# -- dumpsys battery ----------------------------------------------------------------------------


def test_parse_battery_level() -> None:
    assert parse_battery(fixture("dumpsys_battery.txt")) == 87


def test_parse_battery_ignores_capacity_level() -> None:
    assert parse_battery("  Capacity level: -1\n") is None


def test_parse_battery_scales_to_percent() -> None:
    assert parse_battery("  level: 40\n  scale: 50\n") == 80


@pytest.mark.parametrize("out", ["", "  level: 250\n  scale: 100\n", "  level: x\n"])
def test_parse_battery_gives_none_for_unusable_output(out: str) -> None:
    assert parse_battery(out) is None


# -- tail ---------------------------------------------------------------------------------------


def test_tail_command_is_one_based() -> None:
    # `tail -c +K` starts at the K-th byte, counting from 1 (checked on a phone).
    assert tail_command("/sdcard/a.jpg", 0) == "tail -c +1 /sdcard/a.jpg"
    assert tail_command("/sdcard/a.jpg", 1) == "tail -c +2 /sdcard/a.jpg"
    assert tail_command("/sdcard/a b.jpg", 4096) == "tail -c +4097 '/sdcard/a b.jpg'"


def test_tail_command_rejects_negative_offsets() -> None:
    with pytest.raises(ValueError):
        tail_command("/sdcard/a.jpg", -1)


# -- sha256 -------------------------------------------------------------------------------------


def test_sha256_script_has_one_line_per_path_and_never_echoes_names() -> None:
    script = build_sha256_script(["/sdcard/a.jpg", "/sdcard/b c.jpg"])
    assert script.splitlines() == [
        "sha256sum 2>/dev/null < /sdcard/a.jpg || echo '!'",
        "sha256sum 2>/dev/null < '/sdcard/b c.jpg' || echo '!'",
    ]


def test_sha256_script_silences_redirect_errors_before_opening_the_file() -> None:
    # Redirections apply left to right: stderr must point at /dev/null before `<` can fail, or
    # the shell's "can't open <name>" message (which holds the name) would reach the output.
    for line in build_sha256_script(["/sdcard/missing"]).splitlines():
        assert line.index("2>/dev/null") < line.index("<")


def test_sha256_maps_by_position_with_hostile_names() -> None:
    names = ["/sdcard/two\nlines  -", "/sdcard/back\\slash", "/sdcard/gone", "/sdcard/caf\udce9"]
    out = f"{HASH_A}  -\n{HASH_B}  -\n!\n{HASH_A.upper()}  -\n"
    assert parse_sha256_output(out, len(names)) == [HASH_A, HASH_B, None, HASH_A]


def test_sha256_output_with_the_wrong_line_count_is_an_error() -> None:
    with pytest.raises(RemoteError, match="2 lines for 3 files"):
        parse_sha256_output(f"{HASH_A}  -\n!\n", 3)
    with pytest.raises(RemoteError):
        parse_sha256_output(f"{HASH_A}  -\n!\n!\n!\n", 3)


@pytest.mark.parametrize(
    "line",
    ["sh: can't open", "abc  -", f"{HASH_A}  /sdcard/x", f"{HASH_A}", "", "!!", f"{HASH_A}x  -"],
)
def test_sha256_output_with_an_unexpected_line_is_an_error(line: str) -> None:
    with pytest.raises(RemoteError):
        parse_sha256_output(f"{HASH_A}  -\n{line}\n", 2)


def test_sha256_output_for_no_files() -> None:
    assert parse_sha256_output("", 0) == []
    assert build_sha256_script([]) == ""


# -- quoting ------------------------------------------------------------------------------------

_HOSTILE = "'\" \n\t$`\\!*?;&|<>(){}[]~#=%-éअ😀"
_names = st.one_of(
    st.binary(min_size=1, max_size=30)
    .filter(lambda b: b"\0" not in b and b"/" not in b)
    .map(lambda b: b.decode("utf-8", "surrogateescape")),
    st.text(alphabet=st.sampled_from(_HOSTILE + "abc"), min_size=1, max_size=30).filter(
        lambda s: "/" not in s
    ),
)


@given(name=_names, offset=st.integers(min_value=0, max_value=2**40))
def test_every_shell_path_is_quoted(name: str, offset: int) -> None:
    path = f"/sdcard/Download/{name}"
    # A POSIX shell splits the tail command back into exactly these words.
    assert shlex.split(tail_command(path, offset)) == ["tail", "-c", f"+{offset + 1}", path]
    paths = [path, "/sdcard/plain.jpg", path + "2"]
    words = shlex.split(build_sha256_script(paths))
    assert len(words) == 7 * len(paths)
    for i, expected in enumerate(paths):
        assert words[7 * i : 7 * i + 7] == [
            "sha256sum",
            "2>/dev/null",
            "<",
            expected,
            "||",
            "echo",
            "!",
        ]
    # The phone receives the original bytes of the name.
    raw = path.encode("utf-8", "surrogateescape")
    if "'" not in name:
        assert raw in encode_command(tail_command(path, offset))
