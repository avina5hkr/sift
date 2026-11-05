import subprocess
from collections.abc import Sequence
from pathlib import Path

from sift.services.usblink import android_usb_present, link_speed_mbps

FIX = Path(__file__).parent / "fixtures"
SPHOST = ["system_profiler", "-json", "SPUSBHostDataType"]
IOUSB = ["ioreg", "-p", "IOUSB", "-l", "-w0"]
IFACES = ["ioreg", "-r", "-c", "IOUSBHostInterface", "-l", "-w0"]


def _fx(name: str) -> str:
    return (FIX / name).read_text()


def _ok(out: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, out, "")


class FakeRun:
    """Runner answering per command; a value that is an Exception is raised instead."""

    def __init__(self, **by_cmd: object) -> None:
        self.by_cmd = by_cmd
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert timeout <= 10
        self.calls.append(list(args))
        v = self.by_cmd[args[0]]
        if isinstance(v, BaseException):
            raise v
        assert isinstance(v, subprocess.CompletedProcess)
        return v


def _run(sp: object, io: object) -> FakeRun:
    return FakeRun(system_profiler=sp, ioreg=io)


def test_speed_from_system_profiler_json():
    run = _run(_ok(_fx("sphost.json")), OSError("not used"))
    assert link_speed_mbps("TESTSERIAL01", run=run) == 480
    assert run.calls == [SPHOST]


def test_speed_5_gbps_phone_nested_under_a_hub_is_its_own_speed():
    run = _run(_ok(_fx("sphost.json")), OSError("not used"))
    assert link_speed_mbps("TESTSERIAL02", run=run) == 5000


def test_drive_is_not_mistaken_for_the_phone():
    run = _run(_ok(_fx("sphost.json")), _ok(_fx("ioreg_iousb.txt")))
    # the phone is found by its serial, never as "the fastest/only removable device"
    assert link_speed_mbps("TESTSERIAL01", run=run) == 480
    assert link_speed_mbps("TESTDRIVE01", run=run) == 5000  # only when asked for by serial
    assert link_speed_mbps("NOSUCHSERIAL", run=run) is None


def test_unknown_serial_with_empty_everything_is_none():
    run = _run(_ok(_fx("sphost.json")), _ok(""))
    assert link_speed_mbps("NOSUCHSERIAL", run=run) is None


def test_no_serial_returns_none_without_running_anything():
    run = _run(OSError("no"), OSError("no"))
    assert link_speed_mbps(None, run=run) is None
    assert link_speed_mbps("", run=run) is None
    assert run.calls == []


def test_empty_json_falls_back_to_ioreg():
    run = _run(_ok('{"SPUSBHostDataType": []}'), _ok(_fx("ioreg_iousb.txt")))
    assert link_speed_mbps("TESTSERIAL01", run=run) == 480
    assert run.calls == [SPHOST, IOUSB]


def test_ioreg_speed_codes():
    run = _run(_ok(""), _ok(_fx("ioreg_iousb.txt")))
    assert link_speed_mbps("TESTSERIAL02", run=run) == 5000
    text = _fx("ioreg_iousb.txt")
    for code, mbps in {1: 12, 2: 480, 3: 5000, 4: 10000}.items():
        patched = text.replace('"Device Speed" = 2', f'"Device Speed" = {code}')
        assert link_speed_mbps("TESTSERIAL01", run=_run(_ok(""), _ok(patched))) == mbps
    unknown = text.replace('"Device Speed" = 2', '"Device Speed" = 9')
    assert link_speed_mbps("TESTSERIAL01", run=_run(_ok(""), _ok(unknown))) is None


def test_speed_text_variants():
    def with_speed(s: str) -> FakeRun:
        j = _fx("sphost.json").replace("480 Mb/s", s)
        return _run(_ok(j), OSError("x"))

    assert link_speed_mbps("TESTSERIAL01", run=with_speed("10 Gb/s")) == 10000
    assert link_speed_mbps("TESTSERIAL01", run=with_speed("12 Mb/s")) == 12
    assert link_speed_mbps("TESTSERIAL01", run=with_speed("Up to 480 Mb/s")) == 480
    assert link_speed_mbps("TESTSERIAL01", run=with_speed("up to 5 Gb/s")) == 5000


def test_unparsable_speed_text_falls_back():
    j = _fx("sphost.json").replace("480 Mb/s", "fast")
    run = _run(_ok(j), _ok(_fx("ioreg_iousb.txt")))
    assert link_speed_mbps("TESTSERIAL01", run=run) == 480


def test_garbage_json_and_nonzero_exit_fall_back():
    io = _ok(_fx("ioreg_iousb.txt"))
    assert link_speed_mbps("TESTSERIAL01", run=_run(_ok("not json {"), io)) == 480
    assert link_speed_mbps("TESTSERIAL01", run=_run(_ok("[1, 2]"), io)) == 480
    assert link_speed_mbps("TESTSERIAL01", run=_run(_ok('{"SPUSBHostDataType": 3}'), io)) == 480
    bad = subprocess.CompletedProcess([], 1, _fx("sphost.json"), "boom")
    assert link_speed_mbps("TESTSERIAL01", run=_run(bad, io)) == 480


def test_timeout_and_oserror_fall_back():
    io = _ok(_fx("ioreg_iousb.txt"))
    t = subprocess.TimeoutExpired(["system_profiler"], 10)
    assert link_speed_mbps("TESTSERIAL01", run=_run(t, io)) == 480
    assert link_speed_mbps("TESTSERIAL01", run=_run(OSError("missing"), io)) == 480


def test_both_sources_fail_is_none():
    assert link_speed_mbps("TESTSERIAL01", run=_run(OSError("a"), OSError("b"))) is None
    t = subprocess.TimeoutExpired(["x"], 10)
    assert link_speed_mbps("TESTSERIAL01", run=_run(t, t)) is None
    garbage = _ok("\x00\xff garbage")
    assert link_speed_mbps("TESTSERIAL01", run=_run(garbage, garbage)) is None


def test_missing_tools_give_none_and_false():
    def boom(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(args[0])

    assert link_speed_mbps("TESTSERIAL01", run=boom) is None
    assert android_usb_present(run=boom) is False


def test_invalid_utf8_output_is_handled():
    bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    io = _ok(_fx("ioreg_iousb.txt"))
    assert link_speed_mbps("TESTSERIAL01", run=_run(bad, io)) == 480  # falls back
    assert link_speed_mbps("TESTSERIAL01", run=_run(bad, bad)) is None
    assert android_usb_present(FakeRun(ioreg=bad)) is False


def test_serial_less_hub_before_child_device_in_ioreg():
    # the fixture's hub has no serial and precedes the phone nested under it
    text = _fx("ioreg_iousb.txt")
    assert text.index("Test Hub@") < text.index("TESTSERIAL02")
    assert link_speed_mbps("TESTSERIAL02", run=_run(_ok(""), _ok(text))) == 5000


def test_android_usb_present_vendor_a():
    run = FakeRun(ioreg=_ok(_fx("ioreg_interfaces_vendor_a.txt")))
    assert android_usb_present(run) is True
    assert run.calls == [IFACES]


def test_android_usb_present_vendor_b():
    assert android_usb_present(FakeRun(ioreg=_ok(_fx("ioreg_interfaces_vendor_b.txt")))) is True


def test_android_usb_present_false_without_adb_interface():
    # a drive, plus a vendor-specific interface whose protocol is not 1, plus a non-interface node
    assert android_usb_present(FakeRun(ioreg=_ok(_fx("ioreg_interfaces_no_adb.txt")))) is False


def test_android_usb_present_false_on_failure_or_garbage():
    assert android_usb_present(FakeRun(ioreg=_ok(""))) is False
    assert android_usb_present(FakeRun(ioreg=OSError("x"))) is False
    assert android_usb_present(FakeRun(ioreg=subprocess.TimeoutExpired(["ioreg"], 10))) is False
    bad = subprocess.CompletedProcess([], 1, _fx("ioreg_interfaces_vendor_a.txt"), "")
    assert android_usb_present(FakeRun(ioreg=bad)) is False
