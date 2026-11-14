"""App probes on the phone (spec 10C): the commands, and parsers for what they print.

Every probe is best effort: a parser given text it does not understand returns what it could
read (often nothing), and callers show "unavailable". Package names are checked against the
Android package grammar before they are ever put into a command.
"""

import re
from dataclasses import dataclass

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
PLAY_STORE = "com.android.vending"

PROBES = {
    "packages": "pm list packages -3 -i 2>/dev/null",
    "diskstats": "dumpsys diskstats 2>/dev/null",
    "usagestats": "dumpsys usagestats 2>/dev/null",
    "versions": "dumpsys package packages 2>/dev/null | grep -E '^  Package \\[|versionName='",
}


def valid_package(name: str) -> bool:
    return bool(PACKAGE_RE.match(name)) and len(name) <= 255


@dataclass
class AppInfo:
    package: str
    installer: str | None = None
    version: str | None = None
    app_bytes: int | None = None
    data_bytes: int | None = None
    cache_bytes: int | None = None
    last_used: int | None = None  # Unix seconds


def parse_packages(text: str) -> dict[str, str | None]:
    """`package:com.x  installer=com.android.vending` -> {package: installer}."""
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        m = re.match(r"\s*package:(\S+)(?:\s+installer=(\S+))?", line)
        if m and valid_package(m.group(1)):
            inst = m.group(2)
            out[m.group(1)] = None if inst in (None, "null") else inst
    return out


def _array(text: str, key: str) -> list[str]:
    m = re.search(rf"{re.escape(key)}:\s*\[(.*?)\]", text, re.S)
    if not m:
        return []
    return [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()]


def parse_diskstats(text: str) -> dict[str, tuple[int | None, int | None, int | None]]:
    """`Package Names: [...]`, `App Sizes: [...]`, `App Data Sizes: [...]`, `Cache Sizes: [...]`."""
    names = _array(text, "Package Names")
    cols = [_array(text, k) for k in ("App Sizes", "App Data Sizes", "Cache Sizes")]

    def num(values: list[str], i: int) -> int | None:
        try:
            return int(values[i])
        except IndexError, ValueError:
            return None

    return {n: (num(cols[0], i), num(cols[1], i), num(cols[2], i)) for i, n in enumerate(names)}


def parse_usagestats(text: str) -> dict[str, int]:
    """Newest `lastTimeUsed` per package, from lines such as
    `package=com.x totalTimeUsed="1:02" lastTimeUsed="2024-03-01 10:11:12"` (local time) or with
    epoch milliseconds."""
    import datetime as dt

    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.search(r"package=(\S+).*?lastTimeUsed=\"?([^\"\s]+(?: [0-9:]+)?)\"?", line)
        if not m or not valid_package(m.group(1)):
            continue
        raw = m.group(2)
        when: int | None = None
        if raw.isdigit():
            when = int(raw) // 1000 if len(raw) > 11 else int(raw)
        else:
            try:
                when = int(dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp())
            except ValueError:
                when = None
        if when and when > 0:
            out[m.group(1)] = max(out.get(m.group(1), 0), when)
    return out


def parse_versions(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"\s*Package \[(\S+)\]", line)
        if m:
            current = m.group(1)
            continue
        v = re.search(r"versionName=(\S+)", line)
        if v and current and current not in out:
            out[current] = v.group(1)
    return out


def parse_paths(text: str) -> list[str]:
    return [
        line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("package:")
    ]
