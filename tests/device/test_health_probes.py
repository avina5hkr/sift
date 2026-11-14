"""M14: health probe parsers on synthetic Android 14, 15 and 16 output (spec 10D)."""

import pytest

from sift.device import fake_health as fx
from sift.device.health import (
    PROBES,
    parse_battery,
    parse_bugreportz,
    parse_count,
    parse_data_free,
    parse_df,
    parse_meminfo,
    parse_props,
    parse_telephony,
    parse_thermal,
    parse_top,
    parse_wifi,
)

VERSIONS = ["14", "15", "16"]


@pytest.mark.parametrize("v", VERSIONS)
def test_props_df_and_storage(v: str) -> None:
    props = parse_props(fx.props(v))
    assert props["ro.build.version.release"] == v
    assert props["ro.build.version.security_patch"].count("-") == 2
    vols = parse_df(fx.df(v))
    total, used = fx.DF[v]
    assert vols["/data"].total == total * 1024 and vols["/data"].used == used * 1024
    assert "/storage/emulated" in vols
    free = parse_data_free(fx.diskstats_head(v))
    assert free == ((total - used) * 1024, total * 1024)


@pytest.mark.parametrize("v", VERSIONS)
def test_memory_and_cpu(v: str) -> None:
    mem = parse_meminfo(fx.meminfo(v))  # PSS on 14, RSS on 15 and 16
    assert mem.total == 7_812_345 * 1024 and mem.free == 2_100_000 * 1024
    assert [n for n, _ in mem.by_process[:2]] == ["com.example.social", "system"]
    assert len(mem.by_process) == len(fx.MEMINFO_PROCS)  # the OOM section is not read
    cpu = parse_top(fx.top(v))  # "S[%CPU]" on 14 and 15, separate columns on 16
    assert cpu[0] == ("com.example.social", 35.0) and cpu[1] == ("system_server", 7.5)


@pytest.mark.parametrize("v", VERSIONS)
def test_battery_heat_and_radio(v: str) -> None:
    b = parse_battery(fx.battery(v))
    level, health, temp = fx.BATTERY[v]
    assert b.level == level and b.temperature_c == temp / 10 and b.charging
    assert b.health == ("overheating" if health == 3 else "good")
    assert parse_thermal(fx.thermal(v)) == ["none", "light", "moderate"][fx.THERMAL[v]]
    radio = parse_telephony(fx.telephony(v))
    assert radio.signal_level == {"14": 3, "15": 1, "16": 0}[v]
    assert radio.voice == ("OUT_OF_SERVICE" if v == "16" else "IN_SERVICE")
    w = parse_wifi(fx.wifi(v))
    if v == "16":
        assert w.enabled is False and w.rssi is None
    else:
        assert w.enabled and w.rssi == -58 and w.link_mbps == 866 and w.frequency_mhz == 5180


def test_garbage_gives_nothing_not_an_error() -> None:
    junk = "\x00<<garbage>>\nFilesystem\nPID\nThermal Status: 99\nlevel: x"
    assert parse_props(junk) == {} and parse_df(junk) == {} and parse_data_free(junk) is None
    assert parse_meminfo(junk).by_process == [] and parse_top(junk) == []
    assert parse_battery(junk).level is None and parse_thermal(junk) is None
    assert parse_telephony(junk).signal_level is None and parse_wifi(junk).enabled is None
    assert parse_count("0\n") == 0 and parse_count("error") is None


def test_bugreportz_progress_and_result() -> None:
    assert parse_bugreportz("BEGIN:/x\nPROGRESS:10/100\nPROGRESS:40/100\n") == (40, 100, None, None)
    assert (
        parse_bugreportz("PROGRESS:100/100\nOK:/bugreports/b-1.zip\n")[2] == "/bugreports/b-1.zip"
    )
    assert parse_bugreportz("FAIL:could not create zip\n")[3] == "could not create zip"


def test_probes_are_read_only_commands() -> None:
    writes = ("rm ", "pm ", "am ", "settings put", "setprop", ">", "reboot", "svc ")
    for cmd in PROBES.values():
        assert not any(w in cmd.replace("2>/dev/null", "") for w in writes), cmd
