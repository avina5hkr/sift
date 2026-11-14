"""The phone health report (spec 10D): one pass of read-only probes, then plain verdicts.

Each probe is optional; an area whose probes all failed says "unavailable" and the rest of the
report stands. The report explains likely causes of lag and network trouble. It never claims a
hardware fault: when the SIM or the signal looks bad it says a reset may not fix this and points
to the phone maker's diagnostics, testing the SIM in another phone, and a service centre.
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from sift.core.config import Config
from sift.core.db import Database
from sift.core.files import report_folder, write_new
from sift.device import health as h
from sift.device.apps import parse_diskstats, parse_packages
from sift.device.transport import Transport

Level = Literal["ok", "warn", "bad", "unavailable"]
GB = 1_000_000_000
NETWORK_ADVICE = (
    "A reset may not fix this. Try the phone maker's diagnostics app (Samsung Members on a "
    "Samsung), test the SIM in another phone, or visit a service centre."
)
UNAVAILABLE = "Unavailable on this phone."


@dataclass
class Area:
    key: str
    title: str
    level: Level
    verdicts: list[str]
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    created_at: int
    areas: list[Area]
    unavailable: list[str]  # probe kinds that did not answer

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gb(n: int) -> str:
    return f"{n / GB:.1f} GB"


def _months_since(day: str, now: float) -> int | None:
    try:
        d = date.fromisoformat(day.strip())
    except ValueError:
        return None
    today = datetime.fromtimestamp(now, UTC).date()
    return max(0, (today.year - d.year) * 12 + today.month - d.month)


def _system(props: dict[str, str], now: float) -> Area:
    if not props:
        return Area("system", "System", "unavailable", [UNAVAILABLE])
    release = props.get("ro.build.version.release")
    patch = props.get("ro.build.version.security_patch", "")
    facts = {"android": release, "security_patch": patch or None,
             "build": props.get("ro.build.display.id")}  # fmt: skip
    verdicts = [f"Android {release}." if release else "Android version unavailable."]
    level: Level = "ok"
    months = _months_since(patch, now) if patch else None
    if months is None:
        verdicts.append("Security patch date unavailable.")
    elif months >= 6:
        verdicts.append(f"Security patch is {months} months old. Check for a system update.")
        level = "warn"
    else:
        verdicts.append(f"Security patch is from {patch}.")
    return Area("system", "System", level, verdicts, facts)


def _storage(texts: dict[str, str | None], warn_pct: int) -> Area:
    vols = h.parse_df(texts.get("df") or "")
    data = vols.get("/data")
    total = used = None
    if data and data.total:
        total, used = data.total, data.used
    elif free := h.parse_data_free(texts.get("diskstats") or ""):
        total, used = free[1], free[1] - free[0]
    if not total or used is None:
        return Area("storage", "Storage", "unavailable", [UNAVAILABLE])
    pct = round(100 * used / total)
    facts = {"total": total, "used": used, "free": total - used, "used_pct": pct}
    if pct >= warn_pct:
        return Area("storage", "Storage", "warn",
                    [f"Storage is {pct}% full ({_gb(total - used)} free), which often causes lag. "
                     "Freeing space usually helps."], facts)  # fmt: skip
    return Area(
        "storage",
        "Storage",
        "ok",
        [f"Storage is {pct}% full ({_gb(total - used)} free of {_gb(total)})."],
        facts,
    )


def _memory(texts: dict[str, str | None]) -> Area:
    mem = h.parse_meminfo(texts.get("meminfo") or "")
    cpu = h.parse_top(texts.get("top") or "")
    if not mem.by_process and mem.total is None and not cpu:
        return Area("memory", "Memory and CPU", "unavailable", [UNAVAILABLE])
    verdicts = []
    if mem.total:
        free = f", {_gb(mem.free)} free" if mem.free is not None else ""
        verdicts.append(f"Memory: {_gb(mem.total)}{free}.")
    top5 = mem.by_process[:5]
    if top5:
        verdicts.append(
            "Top 5 apps by memory: " + ", ".join(f"{n} ({p / 1e6:.0f} MB)" for n, p in top5) + "."
        )
    level: Level = "ok"
    if cpu and cpu[0][1] >= 50:
        verdicts.append(f"{cpu[0][0]} is using {cpu[0][1]:.0f}% CPU right now.")
        level = "warn"
    elif cpu:
        verdicts.append(f"Busiest process right now: {cpu[0][0]} ({cpu[0][1]:.0f}% CPU).")
    facts = {"total": mem.total, "free": mem.free, "top_memory": top5, "top_cpu": cpu[:5]}
    return Area("memory", "Memory and CPU", level, verdicts, facts)


def _battery(texts: dict[str, str | None], warn_c: int) -> Area:
    b = h.parse_battery(texts.get("battery") or "")
    if b.level is None and b.health is None and b.temperature_c is None:
        return Area("battery", "Battery", "unavailable", [UNAVAILABLE])
    level: Level = "ok"
    parts = []
    if b.health:
        parts.append(f"Battery health {b.health}")
        if b.health not in ("good", "unknown"):
            level = "warn"
    if b.temperature_c is not None:
        parts.append(f"temperature {b.temperature_c:.0f} C")
        if b.temperature_c >= warn_c:
            level = "warn"
    verdicts = [", ".join(parts).capitalize() + "." if parts else "Battery details unavailable."]
    if b.temperature_c is not None and b.temperature_c >= warn_c:
        verdicts.append("The battery is hot; a hot phone slows itself down.")
    if b.level is not None:
        verdicts.append(f"Charge {b.level}%{' (charging)' if b.charging else ''}.")
    return Area("battery", "Battery", level, verdicts, asdict(b))


def _heat(texts: dict[str, str | None]) -> Area:
    status = h.parse_thermal(texts.get("thermal") or "")
    if status is None:
        return Area("heat", "Heat", "unavailable", [UNAVAILABLE])
    if status == "none":
        return Area("heat", "Heat", "ok", ["Not throttling."], {"thermal": status})
    level: Level = "warn" if status == "light" else "bad"
    return Area("heat", "Heat", level,
                [f"The phone is throttling ({status}): it slows down to cool off."],
                {"thermal": status})  # fmt: skip


def _network(props: dict[str, str], texts: dict[str, str | None]) -> Area:
    radio = h.parse_telephony(texts.get("telephony") or "")
    wifi = h.parse_wifi(texts.get("wifi") or "")
    sims = [s.strip() for s in props.get("gsm.sim.state", "").split(",") if s.strip()]
    types = [t.strip() for t in props.get("gsm.network.type", "").split(",")]
    if not sims and radio.signal_level is None and radio.voice is None and wifi.enabled is None:
        return Area("network", "Network", "unavailable", [UNAVAILABLE])
    verdicts: list[str] = []
    bad = False
    ready = [i for i, s in enumerate(sims) if s in ("READY", "LOADED")]
    if sims:
        if ready:
            net = types[ready[0]] if ready[0] < len(types) else ""
            verdicts.append("SIM ready" + (f" on {net}" if net and net != "Unknown" else "") + ".")
        else:
            verdicts.append(f"No SIM ready ({', '.join(sims).lower()}).")
            bad = True
    if radio.signal_level is not None:
        verdicts.append(f"Signal level {radio.signal_level} of 4.")
        bad = bad or radio.signal_level <= 1
    if radio.voice and radio.voice != "IN_SERVICE":
        verdicts.append(f"Calls: {radio.voice.replace('_', ' ').lower()}.")
        bad = True
    if wifi.enabled is False:
        verdicts.append("Wi-Fi is off.")
    elif wifi.enabled and wifi.rssi is not None:
        speed = f", link {wifi.link_mbps} Mbps" if wifi.link_mbps else ""
        verdicts.append(f"Wi-Fi connected, signal {wifi.rssi} dBm{speed}.")
    elif wifi.enabled:
        verdicts.append("Wi-Fi is on, not connected.")
    if bad:
        verdicts.append(NETWORK_ADVICE)
    facts = {"sims": sims, "network_types": types, "signal_level": radio.signal_level,
             "voice": radio.voice, "data": radio.data, "wifi": asdict(wifi)}  # fmt: skip
    return Area("network", "Network", "bad" if bad else "ok", verdicts, facts)


def _apps(transport: Transport, texts: dict[str, str | None]) -> Area:
    sizes = parse_diskstats(texts.get("diskstats") or "")
    try:
        third = set(parse_packages(transport.apps_probe("packages") or ""))
    except Exception:
        third = set()
    if not sizes:
        return Area("apps", "Apps", "unavailable", [UNAVAILABLE])
    rows = [(p, sum(x or 0 for x in s)) for p, s in sizes.items() if not third or p in third]
    rows.sort(key=lambda r: -r[1])
    top = rows[:10]
    verdicts = [
        "The 10 largest apps: " + ", ".join(f"{p} ({_gb(n)})" for p, n in top) + "."
        if top
        else "No app sizes reported."
    ]
    return Area("apps", "Apps", "ok", verdicts, {"largest": top})


def collect(transport: Transport, config: Config, *, now: float | None = None,
            should_stop: Callable[[], bool] = lambda: False,
            on_progress: Callable[[int, int], None] = lambda d, t: None) -> Report:  # fmt: skip
    """Run every probe once (read-only) and build the report."""
    now = time.time() if now is None else now
    texts: dict[str, str | None] = {}
    kinds = list(h.PROBES)
    for i, kind in enumerate(kinds):
        if should_stop():
            break
        try:
            texts[kind] = transport.health_probe(kind)
        except Exception:
            texts[kind] = None
        on_progress(i + 1, len(kinds))
    props = h.parse_props(texts.get("props") or "")
    cfg = config.health
    areas = [
        _system(props, now),
        _storage(texts, cfg.storage_warn_pct),
        _memory(texts),
        _battery(texts, cfg.battery_temp_warn_c),
        _heat(texts),
        _network(props, texts),
        _apps(transport, texts),
    ]
    missing = [k for k in kinds if not (texts.get(k) or "").strip()]
    return Report(int(now), areas, missing)


def save(db: Database, device_id: int, report: Report) -> int:
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO health_report (device_id, created_at, report) VALUES (?, ?, ?)",
            (device_id, report.created_at, json.dumps(report.as_dict())),
        )
        return int(cur.lastrowid or 0)


def latest(db: Database, device_id: int) -> dict[str, Any] | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT id, report FROM health_report WHERE device_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    if row is None:
        return None
    return {"id": int(row[0]), **json.loads(row[1])}


def markdown(report: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(report["created_at"], UTC).astimezone()
    lines = [f"# Phone health report\n\n{when:%d %b %Y, %H:%M}\n"]
    for a in report["areas"]:
        mark = {"ok": "OK", "warn": "Check", "bad": "Problem", "unavailable": "Unavailable"}
        lines.append(f"## {a['title']} ({mark[a['level']]})\n")
        lines += [f"- {v}" for v in a["verdicts"]]
        lines.append("")
    lines.append("This report reads the phone's own status; it does not diagnose hardware faults.")
    return "\n".join(lines) + "\n"


def export(report: dict[str, Any], roots: list[Path], report_dir: str) -> list[str]:
    """The report as Markdown in `DEST_ROOT/<report_dir>/` on each root; never replaces a file."""
    body = markdown(report).encode()
    stamp = datetime.fromtimestamp(report["created_at"], UTC).astimezone()
    written = []
    for root in roots:
        folder = report_folder(root, report_dir)
        folder.mkdir(parents=True, exist_ok=True)
        written.append(str(write_new(folder / f"health-{stamp:%Y-%m-%d-%H%M%S}.md", body)))
    return written
