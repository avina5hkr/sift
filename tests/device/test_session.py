"""PhoneIdentity (two-part identity) and DeviceSession (the shared transport and its check)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sift.device.fake_phone import FakePhone
from sift.device.fake_transport import FakeTransport
from sift.device.session import DeviceSession, PhoneIdentity, WrongDevice
from sift.device.transport import DeviceInfo, RemoteError, TransportError

maybe = st.one_of(st.none(), st.just(""), st.text(alphabet="abc123", min_size=1, max_size=4))


def test_identity_matches_on_the_serial_alone() -> None:
    assert PhoneIdentity("S1", "A1").matches(PhoneIdentity("S1", "A2"))
    assert PhoneIdentity("S1", None).matches(PhoneIdentity("S1", "A2"))


def test_identity_matches_on_the_android_id_alone() -> None:
    assert PhoneIdentity("S1", "A1").matches(PhoneIdentity("S2", "A1"))
    assert PhoneIdentity(None, "A1").matches(PhoneIdentity("S9", "A1"))


def test_identity_does_not_match_when_both_differ() -> None:
    assert not PhoneIdentity("S1", "A1").matches(PhoneIdentity("S2", "A2"))


@pytest.mark.parametrize("empty", [None, ""])
def test_a_missing_identifier_never_matches_another_missing_one(empty: str | None) -> None:
    assert not PhoneIdentity(empty, "A1").matches(PhoneIdentity(empty, "A2"))
    assert not PhoneIdentity("S1", empty).matches(PhoneIdentity("S2", empty))
    assert not PhoneIdentity(empty, empty).matches(PhoneIdentity(empty, empty))


@given(a=maybe, b=maybe, c=maybe, d=maybe)
def test_matching_is_symmetric_and_needs_a_shared_non_empty_value(
    a: str | None, b: str | None, c: str | None, d: str | None
) -> None:
    one, two = PhoneIdentity(a, b), PhoneIdentity(c, d)
    assert one.matches(two) == two.matches(one)
    shared = (bool(a) and a == c) or (bool(b) and b == d)
    assert one.matches(two) == shared


def test_identity_is_frozen_and_hashable() -> None:
    identity = PhoneIdentity("S1", None)
    with pytest.raises(AttributeError):
        identity.serial = "S2"  # type: ignore[misc]
    assert hash(identity) == hash(PhoneIdentity("S1", None))


def test_updated_with_takes_what_is_newly_reported_and_keeps_the_rest() -> None:
    old = PhoneIdentity("S1", None)
    assert old.updated_with(PhoneIdentity(None, "A1")) == PhoneIdentity("S1", "A1")
    assert old.updated_with(PhoneIdentity("S2", "A1")) == PhoneIdentity("S2", "A1")
    assert old.updated_with(PhoneIdentity(None, None)) == old


def test_a_phone_known_only_by_its_stored_key_matches_on_either_identifier() -> None:
    stored = PhoneIdentity.from_stored_key("KEY1")  # the key may be the serial or the Android ID
    assert stored.matches(PhoneIdentity("KEY1", "A9"))
    assert stored.matches(PhoneIdentity(None, "KEY1"))
    assert not stored.matches(PhoneIdentity("S2", "A2"))
    assert PhoneIdentity.from_stored_key("").is_empty()


def test_the_stored_key_is_the_serial_else_the_android_id() -> None:
    assert PhoneIdentity("S1", "A1").key() == "S1"
    assert PhoneIdentity(None, "A1").key() == "A1"
    assert PhoneIdentity(None, None).key() == ""


def test_from_info_treats_empty_as_missing() -> None:
    info = DeviceInfo("", "m", "14", None, None, None, (), android_id="A1")
    assert PhoneIdentity.from_info(info) == PhoneIdentity(None, "A1")


def make_session(phone: FakePhone | None = None) -> tuple[DeviceSession, FakePhone]:
    phone = phone or FakePhone(serial="S1", android_id="A1")
    transport = FakeTransport(phone)
    identity = PhoneIdentity.from_info(transport.info())
    return DeviceSession(transport, identity, "uuid-1"), phone


def test_check_identity_passes_for_the_same_phone() -> None:
    session, _ = make_session()
    session.check_identity()
    assert session.identity == PhoneIdentity("S1", "A1")
    assert session.device_uuid == "uuid-1"


def test_check_identity_raises_when_a_different_phone_answers() -> None:
    session, phone = make_session()
    phone.serial, phone.android_id = "S2", "A2"
    with pytest.raises(WrongDevice):
        session.check_identity()
    assert session.identity == PhoneIdentity("S1", "A1")  # a stranger is never merged in


def test_check_identity_survives_a_change_of_one_identifier_and_learns_it() -> None:
    session, phone = make_session()
    phone.serial = "S2"
    session.check_identity()
    assert session.identity == PhoneIdentity("S2", "A1")
    phone.android_id = "A2"
    session.check_identity()
    assert session.identity == PhoneIdentity("S2", "A2")


def test_check_identity_learns_an_identifier_it_did_not_have() -> None:
    phone = FakePhone(serial="S1", android_id="A1")
    transport = FakeTransport(phone)
    session = DeviceSession(transport, PhoneIdentity("S1", None), "uuid-1")
    session.check_identity()
    assert session.identity == PhoneIdentity("S1", "A1")


def test_check_identity_with_no_identity_at_all_is_an_error_not_a_match() -> None:
    session, phone = make_session()
    phone.serial, phone.android_id = "", ""
    with pytest.raises(RemoteError):
        session.check_identity()


def test_wrong_device_is_a_transport_error_with_no_phone_text() -> None:
    assert issubclass(WrongDevice, TransportError)
    session, phone = make_session()
    phone.serial, phone.android_id = "S2-SECRET", "A2-SECRET"
    with pytest.raises(WrongDevice) as info:
        session.check_identity()
    assert "SECRET" not in str(info.value)


def test_read_info_returns_the_probe_and_checks_in_the_same_call() -> None:
    session, phone = make_session()
    calls = [e.op for e in session.transport.journal]  # type: ignore[attr-defined]
    info = session.read_info()
    assert info.model == phone.model
    ops = [e.op for e in session.transport.journal]  # type: ignore[attr-defined]
    assert ops[len(calls) :] == ["info"]  # one probe, not two
