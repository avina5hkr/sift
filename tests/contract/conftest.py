"""The `transport` fixture: every contract test runs against the fake and, opt-in, a real phone.

The real phone runs only with `-m realphone` and these environment variables:

- `SIFT_REALPHONE_SERIAL` (required): the phone's adb serial; the param skips without it.
- `SIFT_REALPHONE_ADB_PORT` (default 5037): the adb server's port.
- `SIFT_REALPHONE_SAMPLE_DIR` (default /sdcard/DCIM/Camera): the folder the walk, read and hash
  tests use on the phone, so they do not walk the whole storage.

Everything the suite does on the phone only reads.
"""

import os

import pytest

from sift.device.adb_transport import AdbTransport
from sift.device.fake_phone import build_fake_phone
from sift.device.fake_transport import FakeTransport
from sift.device.transport import Transport

DEFAULT_SAMPLE_DIR = "/sdcard/DCIM/Camera"


def realphone_sample_dir() -> str:
    return os.environ.get("SIFT_REALPHONE_SAMPLE_DIR", DEFAULT_SAMPLE_DIR)


@pytest.fixture(
    params=[
        "fake",
        pytest.param("adb", marks=pytest.mark.realphone),
    ]
)
def transport(request: pytest.FixtureRequest) -> Transport:
    if request.param == "fake":
        return FakeTransport(build_fake_phone())
    serial = os.environ.get("SIFT_REALPHONE_SERIAL")
    if not serial:
        pytest.skip("set SIFT_REALPHONE_SERIAL to run the contract suite on a real phone")
    port = int(os.environ.get("SIFT_REALPHONE_ADB_PORT", "5037"))
    return AdbTransport(serial, port)
