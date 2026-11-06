"""Shell commands the real phone transport runs, and parsers for what they print.

Remote paths are `str` decoded with `surrogateescape`. Every path that goes into a command is
quoted with `shlex.quote` (POSIX single quotes, which Android's `sh` honours), and the finished
command is turned into bytes with `encode_command`, so a name that is not valid UTF-8 reaches
the phone as its original bytes.

All of these commands only read. Nothing here writes to the phone.
"""

import re
import shlex
from dataclasses import dataclass

from sift.device.transport import RemoteError

_SHA_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *]-")
_SHA_FAILED = "!"
_WM_SIZE = re.compile(r"^\s*(?:Physical|Override) size:\s*(\d+)x(\d+)\s*$", re.MULTILINE)
_LEVEL = re.compile(r"^\s*level:\s*(-?\d+)\s*$", re.MULTILINE)
_SCALE = re.compile(r"^\s*scale:\s*(-?\d+)\s*$", re.MULTILINE)
_DF_BLOCK_SIZES = {"1k-blocks": 1024, "1024-blocks": 1024, "512-blocks": 512}
_SECTION_LINE = re.compile(r"@@sift:([a-z_]+)@@")
_ID_TEXT = re.compile(r"[0-9A-Za-z]{1,64}")
_EPOCH_TEXT = re.compile(r"\d{1,12}")
# Values a phone may report for "no serial number" (ro.serialno on some builds).
_NO_SERIAL = frozenset({"", "unknown", "null"})

# One request that probes everything `info()` needs. Each probe prints its own marked section;
# the marker starts on a fresh line even when the previous probe did not end its output with a
# newline, and stderr goes to /dev/null so an error message never lands inside a section.
_INFO_PROBES: tuple[tuple[str, str], ...] = (
    ("model", "getprop ro.product.model"),
    ("release", "getprop ro.build.version.release"),
    ("serial", "getprop ro.serialno"),
    ("android_id", "settings get secure android_id"),
    ("epoch", "date +%s"),
    ("df", "df /sdcard"),
    ("wm", "wm size"),
    ("battery", "dumpsys battery"),
)
INFO_SCRIPT = "\n".join(
    f"printf '\\n@@sift:{name}@@\\n'; {command} 2>/dev/null" for name, command in _INFO_PROBES
)


def encode_command(command: str) -> bytes:
    """The bytes to send for `command`, with surrogate-escaped name bytes restored."""
    return command.encode("utf-8", "surrogateescape")


def tail_command(path: str, offset: int) -> str:
    """Print a file from byte `offset` (0-based) on. `tail -c +K` counts from 1.

    `--` ends the options, so no path can be read as one.
    """
    if offset < 0:
        raise ValueError(f"offset must not be negative: {offset}")
    return f"tail -c +{offset + 1} -- {shlex.quote(path)}"


def build_sha256_script(paths: list[str]) -> str:
    """One line per path: its hash as `<64 hex>  -`, or `!` if it could not be read.

    The file goes in on stdin, so `sha256sum` prints `-` and never the name: a name with a
    newline or a backslash cannot break the parsing, and output is matched to paths by
    position. stderr is pointed at /dev/null before the `<` redirect so that the shell's own
    "can't open" message (which would hold the name) never reaches the output.
    """
    return "\n".join(f"sha256sum 2>/dev/null < {shlex.quote(p)} || echo '!'" for p in paths)


def parse_sha256_output(out: str, n: int) -> list[str | None]:
    """Hashes (lowercase hex) by position, None where the phone printed `!`.

    Raises `RemoteError` unless there are exactly `n` lines, each a hash or `!`.
    """
    lines = out.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [line.removesuffix("\r") for line in lines]
    if len(lines) != n:
        raise RemoteError(f"The phone's checksum output had {len(lines)} lines for {n} files.")
    hashes: list[str | None] = []
    for number, line in enumerate(lines, start=1):
        if line == _SHA_FAILED:
            hashes.append(None)
            continue
        match = _SHA_LINE.fullmatch(line)
        if match is None:
            raise RemoteError(f"The phone's checksum output has an unexpected line {number}.")
        hashes.append(match.group(1).lower())
    return hashes


def parse_df(out: str) -> tuple[int, int] | None:
    """(free, total) bytes from `df <one path>` (toybox), or None if it cannot be read.

    A long filesystem name may push the numbers onto the next line, so the words after the
    header are read as one sequence.
    """
    lines = [line.split() for line in out.splitlines()]
    start = next((i for i, words in enumerate(lines) if words[:1] == ["Filesystem"]), None)
    if start is None or len(lines[start]) < 2:
        return None
    block_size = _DF_BLOCK_SIZES.get(lines[start][1].lower())
    if block_size is None:
        return None
    words = [word for line in lines[start + 1 :] for word in line]
    if len(words) < 6:
        return None
    try:
        total, available = int(words[1]), int(words[3])
    except ValueError:
        return None
    if total <= 0 or available < 0:
        return None
    return available * block_size, total * block_size


def parse_wm_size(out: str) -> tuple[tuple[int, int], ...]:
    """Physical size, then the override size if one is set, from `wm size`."""
    sizes = [(int(w), int(h)) for w, h in _WM_SIZE.findall(out)]
    return tuple((w, h) for w, h in sizes if w > 0 and h > 0)


def parse_battery(out: str) -> int | None:
    """Battery percentage from `dumpsys battery`, or None if it cannot be read."""
    level = _LEVEL.search(out)
    if level is None:
        return None
    scale_match = _SCALE.search(out)
    scale = int(scale_match.group(1)) if scale_match else 100
    if scale <= 0:
        return None
    percent = round(int(level.group(1)) * 100 / scale)
    return percent if 0 <= percent <= 100 else None


def parse_sections(out: str) -> dict[str, str]:
    """The text of each marked section of `INFO_SCRIPT`'s output, by name.

    Text before the first marker is ignored; if a name repeats, the first one wins. A section
    the phone never printed is simply absent, never an error.
    """
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in out.splitlines():
        marker = _SECTION_LINE.fullmatch(line.strip())
        if marker is not None:
            name = marker.group(1)
            current = sections.setdefault(name, []) if name not in sections else None
            continue
        if current is not None:
            current.append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


@dataclass(frozen=True)
class InfoProbe:
    """What `INFO_SCRIPT` printed, field by field; None (or empty) where it was missing."""

    model: str
    release: str
    serial: str | None
    android_id: str | None
    phone_time: int | None
    storage: tuple[int, int] | None  # (free, total) bytes
    screen_sizes: tuple[tuple[int, int], ...]
    battery_pct: int | None


def _first_line(text: str | None) -> str:
    if text is None:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""


def parse_info(out: str) -> InfoProbe:
    """Parse the output of `INFO_SCRIPT`. Never raises: a missing or odd section gives None."""
    sections = parse_sections(out)
    serial = _first_line(sections.get("serial"))
    android_id = _first_line(sections.get("android_id"))
    epoch = _first_line(sections.get("epoch"))
    return InfoProbe(
        model=_first_line(sections.get("model")),
        release=_first_line(sections.get("release")),
        serial=None if serial.lower() in _NO_SERIAL or " " in serial else serial,
        android_id=android_id if _ID_TEXT.fullmatch(android_id) and android_id != "null" else None,
        phone_time=int(epoch) if _EPOCH_TEXT.fullmatch(epoch) else None,
        storage=parse_df(sections.get("df", "")),
        screen_sizes=parse_wm_size(sections.get("wm", "")),
        battery_pct=parse_battery(sections.get("battery", "")),
    )
