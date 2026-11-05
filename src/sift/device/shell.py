"""Shell commands the real phone transport runs, and parsers for what they print.

Remote paths are `str` decoded with `surrogateescape`. Every path that goes into a command is
quoted with `shlex.quote` (POSIX single quotes, which Android's `sh` honours), and the finished
command is turned into bytes with `encode_command`, so a name that is not valid UTF-8 reaches
the phone as its original bytes.

All of these commands only read. Nothing here writes to the phone.
"""

import re
import shlex

from sift.device.transport import RemoteError

_SHA_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *]-")
_SHA_FAILED = "!"
_WM_SIZE = re.compile(r"^\s*(?:Physical|Override) size:\s*(\d+)x(\d+)\s*$", re.MULTILINE)
_LEVEL = re.compile(r"^\s*level:\s*(-?\d+)\s*$", re.MULTILINE)
_SCALE = re.compile(r"^\s*scale:\s*(-?\d+)\s*$", re.MULTILINE)
_DF_BLOCK_SIZES = {"1k-blocks": 1024, "1024-blocks": 1024, "512-blocks": 512}


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
