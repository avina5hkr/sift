"""USB link speed of the connected phone, and whether an adb USB interface is present.

Vendor-neutral by construction: the phone is found by its USB serial number (which equals
the adb serial for a USB-connected Android phone), never by vendor or model name, and "an
Android phone is on USB" means "some device exposes the standard adb interface"
(class 0xFF, subclass 0x42, protocol 0x01). Nothing here raises to callers.
"""

import json
import re
import subprocess
from collections.abc import Iterator

from sift.core.proc import Runner, subprocess_runner

_TIMEOUT_S = 10.0
_SYSTEM_PROFILER = ["system_profiler", "-json", "SPUSBHostDataType"]
_IOREG_DEVICES = ["ioreg", "-p", "IOUSB", "-l", "-w0"]
# Interface nodes carry bInterface{Class,SubClass,Protocol} and the parent device's serial.
_IOREG_INTERFACES = ["ioreg", "-r", "-c", "IOUSBHostInterface", "-l", "-w0"]

_IOREG_SPEED_MBPS = {1: 12, 2: 480, 3: 5000, 4: 10000}
_ADB_INTERFACE = (0xFF, 0x42, 0x01)

_SPEED_RE = re.compile(r"^\s*(?:up to\s+)?(\d+(?:\.\d+)?)\s*([MG])b/s\s*$", re.IGNORECASE)
_ENTRY_RE = re.compile(r"^[\s|]*\+-o .*<class (\w+)")
_PROP_RE = re.compile(r'^[\s|]*"([^"]+)" = (.*?)\s*$')


def _run_text(run: Runner, args: list[str]) -> str | None:
    """Stdout of a successful run, else None (timeout, missing tool, non-zero exit)."""
    try:
        done = run(args, _TIMEOUT_S)
    except subprocess.SubprocessError, OSError, ValueError:  # ValueError: undecodable output
        return None
    if done.returncode != 0 or not isinstance(done.stdout, str):
        return None
    return done.stdout


def _parse_speed_text(text: object) -> int | None:
    if not isinstance(text, str):
        return None
    m = _SPEED_RE.match(text)
    if m is None:
        return None
    mbps = float(m.group(1)) * (1000 if m.group(2) == "G" else 1)
    return int(mbps) if mbps >= 1 else None


def _walk_devices(node: object) -> Iterator[dict[str, object]]:
    """Every device dict in a system_profiler tree (buses, hubs and their `_items`)."""
    if isinstance(node, list):
        for child in node:
            yield from _walk_devices(child)
    elif isinstance(node, dict):
        yield node
        yield from _walk_devices(node.get("_items"))


def _speed_from_system_profiler(run: Runner, serial: str) -> int | None:
    out = _run_text(run, _SYSTEM_PROFILER)
    if out is None:
        return None
    try:
        tree = json.loads(out)
    except ValueError:
        return None
    if not isinstance(tree, dict):
        return None
    for dev in _walk_devices(tree.get("SPUSBHostDataType")):
        if dev.get("USBDeviceKeySerialNumber") == serial:
            speed = _parse_speed_text(dev.get("USBDeviceKeyLinkSpeed"))
            if speed is not None:
                return speed
    return None


def _ioreg_entries(text: str) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield (class name, properties) per `+-o` node of `ioreg` output.

    A node's own properties precede its children's, so for each key the first value wins and
    collection stops at the next node header.
    """
    cls: str | None = None
    props: dict[str, str] = {}
    for line in text.splitlines():
        header = _ENTRY_RE.match(line)
        if header is not None or "+-o " in line:
            if cls is not None:
                yield cls, props
            cls = header.group(1) if header is not None else ""
            props = {}
            continue
        prop = _PROP_RE.match(line)
        if prop is not None:
            props.setdefault(prop.group(1), prop.group(2))
    if cls is not None:
        yield cls, props


def _unquote(value: str | None) -> str | None:
    if value is not None and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _speed_from_ioreg(run: Runner, serial: str) -> int | None:
    out = _run_text(run, _IOREG_DEVICES)
    if out is None:
        return None
    for cls, props in _ioreg_entries(out):
        if cls != "IOUSBHostDevice" or _unquote(props.get("USB Serial Number")) != serial:
            continue
        code = _int(props.get("Device Speed"))
        if code is not None and code in _IOREG_SPEED_MBPS:
            return _IOREG_SPEED_MBPS[code]
    return None


def link_speed_mbps(serial: str | None = None, *, run: Runner = subprocess_runner) -> int | None:
    """Negotiated USB link speed in Mb/s (12, 480, 5000, 10000, ...) of the phone, or None.

    `serial` is the adb serial, matched against the USB serial number. Primary source is
    `system_profiler -json SPUSBHostDataType`; if that fails or does not list the phone, the
    `Device Speed` code from `ioreg -p IOUSB` is used.
    """
    if not serial:
        return None
    speed = _speed_from_system_profiler(run, serial)
    if speed is None:
        speed = _speed_from_ioreg(run, serial)
    return speed


def android_usb_present(run: Runner = subprocess_runner) -> bool:
    """True when any USB device exposes the standard adb interface (FF/42/01)."""
    out = _run_text(run, _IOREG_INTERFACES)
    if out is None:
        return False
    for cls, props in _ioreg_entries(out):
        if cls != "IOUSBHostInterface":
            continue
        triple = (
            _int(props.get("bInterfaceClass")),
            _int(props.get("bInterfaceSubClass")),
            _int(props.get("bInterfaceProtocol")),
        )
        if triple == _ADB_INTERFACE:
            return True
    return False
