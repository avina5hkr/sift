"""M9: Wi-Fi pairing, reconnect, heartbeat, reader count, the delete rule, keep awake."""

import pytest

from sift.device import wifi
from tests.backup.test_run import Rig
from tests.cleanup.test_browse import client


def test_addresses_are_checked() -> None:
    assert wifi.check_address(" 192.168.1.20:37099 ") == "192.168.1.20:37099"
    assert wifi.check_address("[fe80::1]:5555") == "[fe80::1]:5555"
    for bad in ("phone.local:5555", "192.168.1.20", "1.2.3.4:70000", "1.2.3.4:22; rm -rf /",
                "fe80::1:5555"):  # fmt: skip
        with pytest.raises(wifi.WifiError):
            wifi.check_address(bad)


def test_mdns_lines_are_parsed_in_both_shapes() -> None:
    text = (
        "List of discovered mdns services\n"
        "adb-ABC-xyz\t_adb-tls-connect._tcp.\t192.168.1.20:41235\n"
        "adb-ABC-xyz   _adb-tls-pairing._tcp   192.168.1.20:37099\n"
        "printer\t_ipp._tcp\t192.168.1.9:631\n"
        "broken line\n"
    )
    found = wifi.parse_mdns(text)
    assert [(s.kind, s.address) for s in found] == [
        (wifi.CONNECT_SERVICE, "192.168.1.20:41235"),
        (wifi.PAIRING_SERVICE, "192.168.1.20:37099"),
    ]


def fake_server(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> list[str]:
    sent: list[str] = []

    def query(port: int, request: str, timeout_s: float) -> str:
        sent.append(request)
        for prefix, answer in answers.items():
            if request.startswith(prefix):
                return answer
        raise ConnectionError("no answer")

    monkeypatch.setattr(wifi, "_query", query)
    return sent


def test_pair_then_connect_to_the_connect_port_and_remember(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = fake_server(monkeypatch, {
        "host:pair:": "Successfully paired to 192.168.1.20:37099 [guid=adb-ABC]",
        "host:mdns:services": "adb-ABC-xyz\t_adb-tls-connect._tcp\t192.168.1.20:41235\n",
        "host:connect:": "connected to 192.168.1.20:41235",
    })  # fmt: skip
    c, h = client(rig)
    r = c.post(
        "/api/phone/wifi/pair", headers=h, json={"address": "192.168.1.20:37099", "code": "123 456"}
    )
    assert r.json() == {"paired": "192.168.1.20:37099", "connected": "192.168.1.20:41235"}
    assert sent[0] == "host:pair:123456:192.168.1.20:37099"
    assert [(x.name, x.address) for x in wifi.remembered(rig.ctx.db)] == [
        ("adb-ABC-xyz", "192.168.1.20:41235")
    ]
    bad = c.post(
        "/api/phone/wifi/pair", headers=h, json={"address": "192.168.1.20:37099", "code": "12"}
    )
    assert bad.status_code == 409 and "6 digits" in bad.json()["message"]


def test_a_wrong_code_is_explained(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_server(monkeypatch, {"host:pair:": "Failed: Wrong password or connection was dropped."})
    c, h = client(rig)
    r = c.post(
        "/api/phone/wifi/pair", headers=h, json={"address": "192.168.1.20:37099", "code": "111111"}
    )
    assert r.status_code == 409 and "Check the code" in r.json()["message"]
    assert wifi.remembered(rig.ctx.db) == []


def test_reconnect_follows_the_mdns_name_when_the_port_moves(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    wifi.remember(rig.ctx.db, "adb-ABC-xyz", "192.168.1.20:41235", connected=True)
    sent = fake_server(monkeypatch, {
        "host:mdns:services": "adb-ABC-xyz\t_adb-tls-connect._tcp\t192.168.1.20:40000\n",
        "host:connect:192.168.1.20:40000": "connected to 192.168.1.20:40000",
    })  # fmt: skip
    assert wifi.reconnect(rig.ctx.db, 5037) == ["192.168.1.20:40000"]
    assert "host:connect:192.168.1.20:40000" in sent
    assert [x.address for x in wifi.remembered(rig.ctx.db)] == ["192.168.1.20:40000"]


def test_wifi_uses_its_reader_count_and_a_fast_heartbeat(rig: Rig) -> None:
    from sift.backup.run import reader_count

    cfg = rig.ctx.config
    cfg.phone.parallel_reads_usb, cfg.phone.parallel_reads_wifi = 4, 2
    assert reader_count(cfg, rig.session) == 4
    rig.transport.serial = "192.168.1.20:41235"  # type: ignore[attr-defined]
    assert reader_count(cfg, rig.session) == 2
    assert wifi.on_wifi(rig.session)


def test_deleting_over_wifi_needs_the_setting(rig: Rig) -> None:
    from sift.cleanup.deleter import check_execute
    from sift.cleanup.errors import ExecuteRefused

    rig.transport.serial = "adb-ABC-xyz._adb-tls-connect._tcp"  # type: ignore[attr-defined]
    with pytest.raises(ExecuteRefused) as refused:
        check_execute(rig.ctx, 1, rig.session, confirm=True, typed_count=None)
    assert refused.value.code == "wifi"
    c, h = client(rig)
    for path, body in (
        ("/api/delete/preflight", {"source": "cleanup", "ids": [1]}),
        ("/api/apps/uninstall", {"action": "uninstall", "packages": ["com.a.b"], "confirm": True}),
    ):
        r = c.post(path, headers=h, json=body)
        assert r.status_code == 409 and r.json()["code"] == "wifi", (path, r.json())
    rig.ctx.config.phone.allow_delete_over_wifi = True
    with pytest.raises(ExecuteRefused) as other:
        check_execute(rig.ctx, 1, rig.session, confirm=True, typed_count=None)
    assert other.value.code != "wifi"


def test_the_tracker_checks_a_wifi_phone_every_heartbeat(rig: Rig) -> None:
    from sift.device.tracker import REFRESH_S, DeviceTracker

    t = DeviceTracker.__new__(DeviceTracker)
    t._ctx = rig.ctx
    rig.ctx.config.phone.heartbeat_seconds = 10
    assert t._every("192.168.1.20:41235") == 10.0 and t._every("R5CT1234") == REFRESH_S


class FakeCaffeinate:
    def __init__(self, cmd: list[str]) -> None:
        self.cmd, self.running = cmd, True

    def terminate(self) -> None:
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_keep_awake_holds_the_mac_and_restores_the_phone(rig: Rig) -> None:
    from sift.services.keepawake import STAY_ON, KeepAwake, restore_leftover

    spawned: list[FakeCaffeinate] = []

    def spawn(cmd: list[str]) -> FakeCaffeinate:
        spawned.append(FakeCaffeinate(cmd))
        return spawned[-1]

    phone = rig.transport.phone
    phone.settings[STAY_ON] = "0"
    awake = KeepAwake(rig.ctx, spawn)
    with awake.hold(), awake.hold():  # nested: one assertion
        assert len(spawned) == 1 and spawned[0].running and "-w" in spawned[0].cmd
        assert STAY_ON not in [e.path for e in rig.transport.journal if e.mutating]  # off
    assert not spawned[0].running
    rig.ctx.config.phone.keep_awake = True
    with awake.hold():
        assert phone.settings[STAY_ON] == "3"
    assert phone.settings[STAY_ON] == "0"
    # a crash while it was on: the saved value is put back at the next launch
    awake._phone_on()
    assert phone.settings[STAY_ON] == "3"
    assert restore_leftover(rig.ctx) and phone.settings[STAY_ON] == "0"


def test_the_junk_model_trains_once_per_fingerprint(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sift.cleanup import junk

    calls = []
    real = junk.train
    monkeypatch.setattr(junk, "train", lambda *a: calls.append(1) or real(*a))
    junk._cache.clear()
    for _ in range(3):
        junk.score_all(rig.ctx.db, 1, rig.ctx.config, 2, None)
    assert len(calls) == 1
    with rig.ctx.db.tx() as conn:
        junk.bump_labels(conn)  # a label changed: retrain
    junk.score_all(rig.ctx.db, 1, rig.ctx.config, 2, None)
    assert len(calls) == 2


def test_a_leftover_of_one_phone_never_blocks_or_restores_another(rig: Rig) -> None:
    from sift.services.keepawake import SETTING_KEY, STAY_ON, KeepAwake, restore_leftover

    with rig.ctx.db.tx() as conn:  # phone A crashed while kept awake (it had 1)
        conn.execute("INSERT INTO setting (key, value) VALUES (?, '1')", (f"{SETTING_KEY}:other",))
    phone = rig.transport.phone
    phone.settings[STAY_ON] = "0"
    rig.ctx.config.phone.keep_awake = True
    awake = KeepAwake(rig.ctx, lambda cmd: FakeCaffeinate(cmd))
    with awake.hold():
        assert phone.settings[STAY_ON] == "3"
    assert phone.settings[STAY_ON] == "0"  # this phone gets its own value back, not A's
    assert not restore_leftover(rig.ctx)  # A's value still waits for phone A
