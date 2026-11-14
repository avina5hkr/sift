"""Health probes on the phone (spec 10D): the fixed read-only commands and their parsers.

Every probe is optional. A parser given text it does not understand returns what it could read
(often nothing), and the report shows "unavailable". Output shapes differ between Android
versions (14, 15 and 16 are covered by the fixtures); parsers look for keys, not line positions.
"""

import re
from dataclasses import dataclass, field

PROBES = {
    "props": "getprop 2>/dev/null",
    "df": "df -k /data /sdcard 2>/dev/null",
    "diskstats": "dumpsys diskstats 2>/dev/null",
    "meminfo": "dumpsys meminfo 2>/dev/null",
    "top": "top -b -n 1 -m 15 2>/dev/null",
    "battery": "dumpsys battery 2>/dev/null",
    "thermal": "dumpsys thermalservice 2>/dev/null",
    "telephony": "dumpsys telephony.registry 2>/dev/null",
    "wifi": "dumpsys wifi 2>/dev/null | head -c 400000",
    "contacts": "content query --uri content://com.android.contacts/contacts --projection _id"
    " 2>/dev/null | grep -c '^Row:'",
}

_PROP = re.compile(r"^\[([^\]]+)\]:\s*\[(.*)\]\s*$")


def parse_props(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = _PROP.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


@dataclass(frozen=True)
class Volume:
    mount: str
    total: int  # bytes
    used: int
    free: int

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


def parse_df(text: str) -> dict[str, Volume]:
    """`df -k`: one row per mount (a long filesystem name may wrap onto its own line)."""
    out: dict[str, Volume] = {}
    pending = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("Filesystem"):
            continue
        parts = (pending + " " + line).split() if pending else line.split()
        if len(parts) == 1:
            pending = parts[0]
            continue
        pending = ""
        if len(parts) < 6 or not all(p.isdigit() for p in parts[1:4]):
            continue
        total, used, free = (int(p) * 1024 for p in parts[1:4])
        out[parts[-1]] = Volume(parts[-1], total, used, free)
    return out


def parse_data_free(text: str) -> tuple[int, int] | None:
    """`dumpsys diskstats` "Data-Free: 12345K / 67890K total = 18% free" → (free, total) bytes."""
    m = re.search(r"Data-Free:\s*(\d+)K\s*/\s*(\d+)K", text)
    return (int(m.group(1)) * 1024, int(m.group(2)) * 1024) if m else None


@dataclass
class Memory:
    total: int | None = None  # bytes
    free: int | None = None
    used: int | None = None
    by_process: list[tuple[str, int]] = field(default_factory=list)  # (name, PSS bytes)


def _kb(text: str) -> int:
    return int(text.replace(",", "")) * 1024


def parse_meminfo(text: str) -> Memory:
    """`dumpsys meminfo`: totals and the "Total PSS by process" (or RSS on 15+) list."""
    mem = Memory()
    for key, attr in (("Total RAM", "total"), ("Free RAM", "free"), ("Used RAM", "used")):
        m = re.search(rf"{key}:\s*([\d,]+)\s*K", text)
        if m:
            setattr(mem, attr, _kb(m.group(1)))
    section = re.search(
        r"Total (?:PSS|RSS) by process:\s*\n(.*?)(?:\n\s*\n|\nTotal (?:PSS|RSS) by OOM)",
        text,
        re.S,
    )
    if section:
        for line in section.group(1).splitlines():
            m = re.match(r"\s*([\d,]+)K:\s+(\S+)\s+\(pid", line)
            if m:
                mem.by_process.append((m.group(2), _kb(m.group(1))))
    mem.by_process.sort(key=lambda p: -p[1])
    return mem


def parse_top(text: str) -> list[tuple[str, float]]:
    """`top -b -n 1`: (process, %CPU) from the column the header names, highest first."""
    rows: list[tuple[str, float]] = []
    cpu_col = name_col = None
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if "PID" in parts and any(p.endswith("CPU]") or p.endswith("CPU") for p in parts):
            i = next(i for i, p in enumerate(parts) if p.endswith("CPU]") or p.endswith("CPU"))
            # "S[%CPU]" is one header word over two values (state, then %CPU)
            shift = 1 if parts[i].startswith("S[") else 0
            cpu_col = i + shift
            name_col = len(parts) - 1 + shift  # ARGS / NAME / CMD is the last column
            continue
        if cpu_col is None or name_col is None or len(parts) <= cpu_col:
            continue
        try:
            cpu = float(parts[cpu_col].rstrip("%"))
        except ValueError:
            continue
        rows.append((" ".join(parts[name_col:]) if len(parts) > name_col else parts[-1], cpu))
    rows.sort(key=lambda r: -r[1])
    return rows


BATTERY_HEALTH = {
    1: "unknown",
    2: "good",
    3: "overheating",
    4: "dead",
    5: "over voltage",
    6: "failing",
    7: "cold",
}


@dataclass
class Battery:
    level: int | None = None
    health: str | None = None
    temperature_c: float | None = None
    charging: bool | None = None


def parse_battery(text: str) -> Battery:
    b = Battery()
    if m := re.search(r"^\s*level:\s*(\d+)", text, re.M):
        b.level = int(m.group(1))
    if m := re.search(r"^\s*health:\s*(\d+)", text, re.M):
        b.health = BATTERY_HEALTH.get(int(m.group(1)), "unknown")
    if m := re.search(r"^\s*temperature:\s*(-?\d+)", text, re.M):
        b.temperature_c = int(m.group(1)) / 10
    if m := re.search(r"^\s*status:\s*(\d+)", text, re.M):
        b.charging = int(m.group(1)) in (2, 5)  # charging, full
    return b


THERMAL = ["none", "light", "moderate", "severe", "critical", "emergency", "shutdown"]


def parse_thermal(text: str) -> str | None:
    """`dumpsys thermalservice` "Thermal Status: N" → its name."""
    m = re.search(r"Thermal Status:\s*(\d+)", text)
    if not m:
        return None
    n = int(m.group(1))
    return THERMAL[n] if 0 <= n < len(THERMAL) else None


@dataclass
class Radio:
    signal_level: int | None = None  # 0..4
    voice: str | None = None  # IN_SERVICE, OUT_OF_SERVICE, EMERGENCY_ONLY, POWER_OFF
    data: str | None = None


def _radio_block(text: str) -> Radio:
    r = Radio()
    if m := re.search(r"mSignalStrength=SignalStrength:\{(.*)", text):
        levels = [int(x) for x in re.findall(r"\blevel\s*=\s*(\d)", m.group(1))]
        if levels:
            r.signal_level = max(levels)
    if m := re.search(r"[Vv]oiceRegState\s*=\s*\d+\((\w+)\)", text):
        r.voice = m.group(1)
    if m := re.search(r"[Dd]ataRegState\s*=\s*\d+\((\w+)\)", text):
        r.data = m.group(1)
    return r


def parse_telephony(text: str, slot: int | None = None) -> Radio:
    """`dumpsys telephony.registry`: signal level and service states of one SIM slot. The
    output has one `Phone Id=N` block per slot; `slot` picks one (the slot with the ready
    SIM), else the block with the best service and signal is used."""
    parts = re.split(r"^\s*Phone Id=(\d+)\s*$", text, flags=re.M)
    blocks: dict[int, Radio] = {}
    for i in range(1, len(parts) - 1, 2):
        blocks.setdefault(int(parts[i]), _radio_block(parts[i + 1]))
    if not blocks:
        return _radio_block(text)
    if slot is not None and slot in blocks:
        return blocks[slot]
    return max(
        blocks.values(),
        key=lambda r: (
            r.voice == "IN_SERVICE",
            r.signal_level if r.signal_level is not None else -1,
        ),
    )


@dataclass
class Wifi:
    enabled: bool | None = None
    rssi: int | None = None
    link_mbps: int | None = None
    frequency_mhz: int | None = None


def parse_wifi(text: str) -> Wifi:
    w = Wifi()
    if m := re.search(r"Wi-Fi is (enabled|disabled)", text):
        w.enabled = m.group(1) == "enabled"
    info = re.search(r"mWifiInfo (.*)", text)
    if info and "SSID: <unknown ssid>" not in info.group(1):
        line = info.group(1)
        if (m := re.search(r"RSSI:\s*(-?\d+)", line)) and int(m.group(1)) > -127:
            w.rssi = int(m.group(1))
        if m := re.search(r"Link speed:\s*(\d+)", line):
            w.link_mbps = int(m.group(1))
        if m := re.search(r"Frequency:\s*(\d+)", line):
            w.frequency_mhz = int(m.group(1))
    return w


def parse_count(text: str) -> int | None:
    t = text.strip()
    return int(t) if t.isdigit() else None


def parse_bugreportz(text: str) -> tuple[int, int, str | None, str | None]:
    """`bugreportz -p` output so far → (done, total, zip path when finished, error)."""
    done = total = 0
    for m in re.finditer(r"PROGRESS:(\d+)/(\d+)", text):
        done, total = int(m.group(1)), int(m.group(2))
    ok = re.search(r"^OK:(\S+\.zip)\s*$", text, re.M)
    fail = re.search(r"^FAIL:(.*)$", text, re.M)
    return done, total, ok.group(1) if ok else None, fail.group(1).strip() if fail else None
