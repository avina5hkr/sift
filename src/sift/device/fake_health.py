"""Canned, synthetic health-probe output for the fake phone, in the shapes Android 14, 15 and 16
print (no real device data). Each version tells a different story so the verdicts are exercised:
14 is healthy, 15 is full, hot and throttling with a weak signal, 16 has no service.
"""

PROPS = {
    "14": {"release": "14", "patch": "2026-09-01", "sim": "READY", "net": "LTE"},
    "15": {"release": "15", "patch": "2025-12-01", "sim": "READY", "net": "LTE"},
    "16": {"release": "16", "patch": "2026-10-01", "sim": "ABSENT,READY", "net": "Unknown,NR"},
}
DF = {
    "14": (115_712_348, 99_512_620),
    "15": (115_712_348, 108_769_607),
    "16": (231_424_696, 115_712_348),
}
BATTERY = {"14": (85, 2, 312), "15": (41, 3, 415), "16": (64, 2, 298)}
THERMAL = {"14": 0, "15": 2, "16": 0}


def props(v: str) -> str:
    p = PROPS[v]
    return (
        f"[ro.build.version.release]: [{p['release']}]\n"
        f"[ro.build.version.security_patch]: [{p['patch']}]\n"
        "[ro.build.display.id]: [TEST.BUILD.001]\n"
        f"[gsm.sim.state]: [{p['sim']}]\n"
        "[gsm.operator.alpha]: [TestNet]\n"
        f"[gsm.network.type]: [{p['net']}]\n"
        "[persist.sys.locale]: [en-GB]\n"
    )


def df(v: str) -> str:
    total, used = DF[v]
    free = total - used
    pct = round(100 * used / total)
    if v == "16":  # a long device name wraps onto its own line
        first = f"/dev/block/dm-52\n          {total} {used} {free} {pct}% /data\n"
    else:
        first = f"/dev/block/dm-48 {total} {used} {free} {pct}% /data\n"
    return (
        "Filesystem       1K-blocks      Used Available Use% Mounted on\n"
        + first
        + f"/dev/fuse        {total} {used} {free} {pct}% /storage/emulated\n"
    )


def diskstats_head(v: str) -> str:
    total, used = DF[v]
    pct = round(100 * (total - used) / total)
    return (
        "Latency: 2ms [512B Data Write]\n"
        f"Data-Free: {total - used}K / {total}K total = {pct}% free\n"
        "Cache-Free: 0K / 0K total = 0% free\n"
    )


MEMINFO_PROCS = [
    ("com.example.social", 612_345),
    ("system", 401_002),
    ("com.example.launcher", 233_100),
    ("com.example.camera", 190_220),
    ("com.example.music", 120_004),
    ("com.example.notes", 80_500),
]


def meminfo(v: str) -> str:
    kind = "PSS" if v == "14" else "RSS"
    lines = [f"Applications Memory Usage (in Kilobytes):\nUptime: 123456 Realtime: 123456\n\n"
             f"Total {kind} by process:"]  # fmt: skip
    lines += [f"    {kb:,}K: {name} (pid {1000 + i}{' / activities' if i == 0 else ''})"
              for i, (name, kb) in enumerate(MEMINFO_PROCS)]  # fmt: skip
    lines += ["", f"Total {kind} by OOM adjustment:", "    1,000,000K: Native", ""]
    lines += [
        "Total RAM: 7,812,345K (status normal)",
        " Free RAM: 2,100,000K",
        " Used RAM: 5,000,000K",
    ]
    return "\n".join(lines) + "\n"


def top(v: str) -> str:
    head = "Tasks: 812 total,   1 running, 811 sleeping\n  Mem:  7812345K total\n800%cpu  42%user\n"
    if v == "16":
        return head + (
            "  PID USER         PR  NI VIRT  RES  SHR S %CPU %MEM     TIME+ ARGS\n"
            "12001 u0_a201      10 -10  16G 410M 190M S 35.0  5.2   1:02.33 com.example.social\n"
            "  901 system       18  -2  20G 300M 200M S  7.5  3.9  10:22.10 system_server\n"
        )
    return head + (
        "  PID USER         PR  NI VIRT  RES  SHR S[%CPU] %MEM     TIME+ ARGS\n"
        "12001 u0_a201      10 -10  16G 410M 190M S 35.0  5.2   1:02.33 com.example.social\n"
        "  901 system       18  -2  20G 300M 200M S  7.5  3.9  10:22.10 system_server\n"
        "13002 u0_a202      20   0  15G 120M  90M S  0.0  1.5   0:00.40 com.example.notes\n"
    )


def battery(v: str) -> str:
    level, health, temp = BATTERY[v]
    extra = "  Charging state: 1\n  Capacity level: 3\n" if v == "16" else ""
    return (
        "Current Battery Service state:\n  AC powered: false\n  USB powered: true\n"
        f"  Wireless powered: false\n  status: 2\n  health: {health}\n  present: true\n"
        f"  level: {level}\n  scale: 100\n  voltage: 4012\n  temperature: {temp}\n"
        f"  technology: Li-ion\n{extra}"
    )


def thermal(v: str) -> str:
    return (
        "IsStatusOverride: false\nThermalEventListeners:\n\tcallbacks: 2\n"
        f"Thermal Status: {THERMAL[v]}\nCached temperatures:\n"
        "\tTemperature{mValue=33.5, mType=3, mName=battery, mStatus=0}\n"
    )


IN = "{mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), mOperatorAlphaLong=TestNet"
OUT = "{voiceRegState=1(OUT_OF_SERVICE), dataRegState=1(OUT_OF_SERVICE), operatorAlphaLong="


def radio_block(slot: int, state: str, lte: int) -> str:
    return (
        f"  Phone Id={slot}\n"
        f"  mServiceState=0 0 voice home data home {state}}}\n"
        "  mSignalStrength=SignalStrength:{ mCdma=CellSignalStrengthCdma: cdmaDbm=2147483647"
        " level=0 mGsm=CellSignalStrengthGsm: rssi=2147483647 level=0 mLte=CellSignalStrengthLte:"
        f" rssi=-67 rsrp=-98 rsrq=-11 level={lte} parametersUseForLevel=0 mNr=CellSignalStrengthNr:"
        "{ csiRsrp = 2147483647 level = 0 }, primary=CellSignalStrengthLte}\n"
    )


def telephony(v: str) -> str:
    if v == "14":
        return "last known state:\n" + radio_block(0, IN, 3)
    if v == "15":
        return "last known state:\n" + radio_block(0, IN, 1)
    # dual SIM: slot 1 empty, the SIM in slot 2 has no service
    return "last known state:\n" + radio_block(0, OUT, 0) + radio_block(1, OUT, 0)


def wifi(v: str) -> str:
    if v == "16":
        return "Wi-Fi is disabled\nmWifiInfo SSID: <unknown ssid>, RSSI: -127, Link speed: -1Mbps\n"
    return (
        "Wi-Fi is enabled\nVerbose logging is off\n"
        'mWifiInfo SSID: "TestWifi", BSSID: 02:00:00:00:00:00, Supplicant state: COMPLETED,'
        " Wi-Fi standard: 11ax, RSSI: -58, Link speed: 866Mbps, Tx Link speed: 866Mbps,"
        " Frequency: 5180MHz, Net ID: 3\n"
    )


TEXT = {
    "props": props,
    "df": df,
    "meminfo": meminfo,
    "top": top,
    "battery": battery,
    "thermal": thermal,
    "telephony": telephony,
    "wifi": wifi,
}


def canned(android_version: str, kind: str) -> str | None:
    """The fixture text of one probe for an Android version (14, 15, 16; others use 14)."""
    v = android_version if android_version in PROPS else "14"
    fn = TEXT.get(kind)
    return fn(v) if fn else None
