"""The `transport` fixture: every contract test runs against the fake and, opt-in, a real phone.

- `fake`: FakeTransport over the standard fake phone.
- `fake-short-reads`: the same, but `read(n)` returns about a third of `n`, so the suite (and
  anything modelled on it) must loop over short reads as a real stream may need.
- `adb`: a real phone, only with `-m realphone` and `SIFT_REALPHONE_SERIAL` set (see
  `tests/contract/helpers.py` for the other variables). Everything it does on the phone reads.
"""

import pytest

from sift.device.adb_transport import AdbTransport
from sift.device.fake_phone import build_fake_phone
from sift.device.fake_transport import FakeTransport, FaultPlan
from sift.device.transport import Transport
from tests.contract.helpers import realphone_adb_port, realphone_serial


@pytest.fixture(
    params=[
        "fake",
        "fake-short-reads",
        pytest.param("adb", marks=pytest.mark.realphone),
    ]
)
def transport(request: pytest.FixtureRequest) -> Transport:
    if request.param == "fake":
        return FakeTransport(build_fake_phone())
    if request.param == "fake-short-reads":
        return FakeTransport(build_fake_phone(), FaultPlan(short_reads=True))
    serial = realphone_serial()
    if serial is None:
        pytest.skip("set SIFT_REALPHONE_SERIAL to run the contract suite on a real phone")
    return AdbTransport(serial, realphone_adb_port())
