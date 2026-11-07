"""The device picker's two tracker calls: `attached_phones` and `choose_phone`."""

from typing import Any

import pytest

from sift.core.context import AppContext
from sift.device.fake_phone import FakePhone
from sift.device.fake_transport import FakeTransport
from sift.device.tracker import (
    ConnectionState,
    PhoneNotAttached,
    PhoneNotReady,
)
from sift.device.transport import DeviceTimeout
from tests.device.test_tracker import STOP, Rig, rig

A = "AAAA-0001"
B = "BBBB-0002"
S = ConnectionState


def two_phones(ctx: AppContext) -> Rig:
    r = rig(ctx)
    r.phones[A] = FakeTransport(FakePhone(serial=A, model="Alpha"))
    r.phones[B] = FakeTransport(FakePhone(serial=B, model="Beta"))
    return r


def device_rows(ctx: AppContext) -> list[tuple[Any, ...]]:
    with ctx.db.read() as conn:
        return conn.execute("SELECT serial, model FROM device ORDER BY id").fetchall()


def test_attached_phones_lists_every_non_emulator_with_its_model(ctx: AppContext) -> None:
    r = two_phones(ctx)
    seen: list[Any] = []
    r.run(
        [
            {A: "device", B: "device", "emulator-5554": "device"},
            lambda: seen.extend(r.tracker.attached_phones()),
            STOP,
        ]
    )
    assert [(p.serial, p.model, p.ready, p.stored) for p in seen] == [
        (A, "Alpha", True, False),
        (B, "Beta", True, False),
    ]


def test_an_unauthorized_phone_is_listed_but_not_ready(ctx: AppContext) -> None:
    r = two_phones(ctx)
    seen: list[Any] = []
    r.run(
        [
            {A: "device", B: "unauthorized"},
            lambda: seen.extend(r.tracker.attached_phones()),
            STOP,
        ]
    )
    assert [(p.serial, p.ready, p.waiting_for_allow) for p in seen] == [
        (A, True, False),
        (B, False, True),
    ]


def test_choosing_one_of_two_phones_stores_it_and_connects_it(ctx: AppContext) -> None:
    r = two_phones(ctx)
    seen: list[ConnectionState] = []
    r.run(
        [
            {A: "device", B: "device"},
            lambda: seen.append(r.tracker.status().state),  # nothing stored: asks for one phone
            lambda: r.tracker.choose_phone(B),
        ],
        [
            {A: "device", B: "device"},
            lambda: seen.append(r.tracker.status().state),
            STOP,
        ],
    )
    assert seen == [S.no_device, S.connected_usb]
    assert device_rows(ctx) == [(B, "Beta")]
    assert r.tracker.status().serial == B


def test_choosing_a_phone_that_is_not_attached_is_refused(ctx: AppContext) -> None:
    r = two_phones(ctx)
    errors: list[Exception] = []

    def choose() -> None:
        try:
            r.tracker.choose_phone("nope")
        except PhoneNotAttached as e:
            errors.append(e)

    r.run([{A: "device"}, choose, STOP])
    assert len(errors) == 1
    assert device_rows(ctx) == [(A, "Alpha")]  # the single phone was stored by the tracker


def test_choosing_a_phone_that_waits_for_allow_is_refused(ctx: AppContext) -> None:
    r = two_phones(ctx)
    errors: list[Exception] = []

    def choose() -> None:
        try:
            r.tracker.choose_phone(B)
        except PhoneNotReady as e:
            errors.append(e)

    r.run([{A: "device", B: "unauthorized"}, choose, STOP])
    assert len(errors) == 1
    assert [row[0] for row in device_rows(ctx)] == []


def test_switching_makes_the_other_phone_the_stored_one(ctx: AppContext) -> None:
    r = two_phones(ctx)
    seen: list[tuple[ConnectionState, str | None]] = []

    def note() -> None:
        st = r.tracker.status()
        seen.append((st.state, st.serial))

    r.run(
        [
            {A: "device", B: "device"},
            lambda: r.tracker.choose_phone(A),
        ],
        [
            {A: "device", B: "device"},
            note,
            lambda: r.tracker.choose_phone(B),
        ],
        [
            {A: "device", B: "device"},
            note,
            STOP,
        ],
    )
    assert seen == [(S.connected_usb, A), (S.connected_usb, B)]
    assert device_rows(ctx) == [(A, "Alpha"), (B, "Beta")]  # the first phone's row stays


def test_switching_back_reuses_the_phones_own_row(ctx: AppContext) -> None:
    r = two_phones(ctx)
    r.run(
        [
            {A: "device", B: "device"},
            lambda: r.tracker.choose_phone(A),
        ],
        [{A: "device", B: "device"}, lambda: r.tracker.choose_phone(B)],
        [{A: "device", B: "device"}, lambda: r.tracker.choose_phone(A), STOP],
    )
    assert device_rows(ctx) == [(A, "Alpha"), (B, "Beta")]


def test_a_reading_that_fails_is_not_ready(ctx: AppContext) -> None:
    r = two_phones(ctx)
    r.phones[B] = DeviceTimeout("slow")
    errors: list[Exception] = []

    def choose() -> None:
        with pytest.raises(PhoneNotReady) as e:
            r.tracker.choose_phone(B)
        errors.append(e.value)

    r.run([{A: "device", B: "device"}, choose, STOP])
    assert len(errors) == 1
