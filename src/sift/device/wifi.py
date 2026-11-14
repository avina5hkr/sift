"""Wi-Fi adb (spec 6, Android 11+): pairing, connecting and finding phones over mDNS.

Everything goes through the adb server's own host services, the same requests the `adb pair`,
`adb connect` and `adb mdns services` commands send, so no adb command line is run. Paired
phones are remembered (`wifi_phone`) and reconnected at launch when they are on the network.
"""

import ipaddress
import logging
import re
import time
from dataclasses import dataclass

from sift.core.db import Database
from sift.device import adbwire

log = logging.getLogger("sift.device")

HOST = "127.0.0.1"
PAIR_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 15.0
MDNS_TIMEOUT_S = 5.0
CONNECT_SERVICE = "_adb-tls-connect._tcp"
PAIRING_SERVICE = "_adb-tls-pairing._tcp"
_CODE = re.compile(r"\d{6}")


class WifiError(Exception):
    """Pairing or connecting over Wi-Fi did not work; the message says what to try."""


@dataclass(frozen=True)
class Service:
    name: str  # the mDNS instance name, e.g. adb-SERIAL-xyz
    kind: str  # _adb-tls-connect._tcp or _adb-tls-pairing._tcp
    address: str  # host:port


def check_address(address: str) -> str:
    """`host:port` with an IP address (IPv4 or [IPv6]) and a port, or WifiError."""
    text = address.strip()
    m = re.fullmatch(r"\[?([0-9A-Fa-f:.]+)\]?:(\d{1,5})", text)
    if m is None:
        raise WifiError(
            "Enter the IP address and port shown on the phone, like 192.168.1.20:37099."
        )
    try:
        ip = ipaddress.ip_address(m.group(1))
    except ValueError as e:
        raise WifiError("That is not an IP address.") from e
    port = int(m.group(2))
    if not 1 <= port <= 65535:
        raise WifiError("The port must be between 1 and 65535.")
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


def _query(port: int, request: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    sock = adbwire.connect(HOST, port, timeout_s)
    try:
        adbwire.host_request(sock, request, deadline=deadline)
        return adbwire.read_host_string(sock, deadline=deadline)
    finally:
        sock.close()


def pair(port: int, address: str, code: str) -> str:
    """Pair with the phone's "Pair device with pairing code" screen; returns adb's answer."""
    target = check_address(address)
    code = code.strip().replace(" ", "")
    if not _CODE.fullmatch(code):
        raise WifiError("The pairing code is the 6 digits shown on the phone.")
    try:
        answer = _query(port, f"host:pair:{code}:{target}", PAIR_TIMEOUT_S)
    except Exception as e:
        raise WifiError(f"Pairing did not work: {e}") from e
    if "successfully paired" not in answer.lower():
        raise WifiError(
            "Pairing did not work. Check the code and the address on the phone's pairing "
            f"screen; they change each time it opens. ({answer.strip() or 'no answer'})"
        )
    return answer.strip()


def connect(port: int, address: str) -> str:
    """Connect to the phone's Wireless debugging address; returns adb's answer."""
    target = check_address(address)
    try:
        answer = _query(port, f"host:connect:{target}", CONNECT_TIMEOUT_S)
    except Exception as e:
        raise WifiError(f"Connecting did not work: {e}") from e
    low = answer.lower()
    if "connected to" not in low or "failed" in low or "cannot" in low:
        raise WifiError(
            "The phone did not accept the connection. Is Wireless debugging still on, and the "
            f"phone on the same network? ({answer.strip() or 'no answer'})"
        )
    return answer.strip()


def parse_mdns(text: str) -> list[Service]:
    """`host:mdns:services` lines: NAME<tab>SERVICE<tab>ADDRESS (spaces in older versions)."""
    out = []
    for line in text.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 3:
            continue
        name, kind, address = parts[0].strip(), parts[1].strip().rstrip("."), parts[-1].strip()
        if kind not in (CONNECT_SERVICE, PAIRING_SERVICE):
            continue
        try:
            out.append(Service(name, kind, check_address(address)))
        except WifiError:
            continue
    return out


def services(port: int) -> list[Service]:
    """Phones announcing Wireless debugging on this network (empty when mDNS cannot answer)."""
    try:
        return parse_mdns(_query(port, "host:mdns:services", MDNS_TIMEOUT_S))
    except Exception as e:
        log.info("mDNS discovery did not answer: %s", e)
        return []


# -- remembered phones ---------------------------------------------------------------------


@dataclass(frozen=True)
class Remembered:
    name: str  # mDNS name of the connect service ('' when connected by address only)
    address: str
    paired_at: int
    last_connected: int | None


def remember(db: Database, name: str, address: str, *, connected: bool) -> None:
    now = int(time.time())
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO wifi_phone (name, address, paired_at, last_connected) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (name, address) DO UPDATE SET "
            "last_connected = COALESCE(excluded.last_connected, last_connected)",
            (name, address, now, now if connected else None),
        )


def remembered(db: Database) -> list[Remembered]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT name, address, paired_at, last_connected FROM wifi_phone "
            "ORDER BY last_connected DESC NULLS LAST, paired_at DESC"
        ).fetchall()
    return [Remembered(str(r[0]), str(r[1]), int(r[2]), r[3]) for r in rows]


def forget(db: Database, name: str, address: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM wifi_phone WHERE name = ? AND address = ?", (name, address))


def reconnect(db: Database, port: int) -> list[str]:
    """Connect to each remembered phone that is on the network now: by its mDNS name (the port
    changes whenever Wireless debugging is toggled), else at its last address. Returns the
    addresses connected."""
    known = remembered(db)
    if not known:
        return []
    found = {s.name: s.address for s in services(port) if s.kind == CONNECT_SERVICE}
    done = []
    for r in known:
        address = found.get(r.name) if r.name else None
        for target in dict.fromkeys(a for a in (address, r.address) if a):
            try:
                connect(port, target)
            except WifiError as e:
                log.info("Wi-Fi reconnect to %s did not work: %s", target, e)
                continue
            if target != r.address:  # the port moved: keep only the new address
                forget(db, r.name, r.address)
            remember(db, r.name, target, connected=True)
            done.append(target)
            break
    return done


def pair_and_connect(db: Database, port: int, address: str, code: str) -> dict[str, str | None]:
    """Pair, then connect to the phone's connect service on the same IP when mDNS finds it
    (the connect port differs from the pairing port). Returns what happened."""
    pair(port, address, code)
    ip = check_address(address).rsplit(":", 1)[0]
    for s in services(port):
        if s.kind == CONNECT_SERVICE and s.address.rsplit(":", 1)[0] == ip:
            connect(port, s.address)
            remember(db, s.name, s.address, connected=True)
            return {"paired": address, "connected": s.address}
    return {"paired": address, "connected": None}


WIFI_DELETE_MESSAGE = (
    "Deleting from the phone needs a USB cable. Connect it over USB, or turn on "
    '"Allow delete over Wi-Fi" in Settings, Phone.'
)


def on_wifi(session: object | None) -> bool:
    """True when the connected phone is reached over Wi-Fi adb."""
    from sift.device.adb_transport import is_wifi_serial

    transport = getattr(session, "transport", None)
    return is_wifi_serial(str(getattr(transport, "serial", "") or ""))


def delete_blocked(allow_over_wifi: bool, session: object | None) -> str | None:
    """Why a phone delete (or app removal) is refused on this link, or None (spec 6)."""
    return WIFI_DELETE_MESSAGE if on_wifi(session) and not allow_over_wifi else None
