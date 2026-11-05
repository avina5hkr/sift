"""The `transport` fixture: every contract test runs against the fake and, opt-in, a real phone."""

import pytest

from sift.device.fake_phone import build_fake_phone
from sift.device.fake_transport import FakeTransport
from sift.device.transport import Transport


@pytest.fixture(
    params=[
        "fake",
        pytest.param("adb", marks=pytest.mark.realphone),
    ]
)
def transport(request: pytest.FixtureRequest) -> Transport:
    if request.param == "fake":
        return FakeTransport(build_fake_phone())
    pytest.skip("AdbTransport arrives in Task 5")
