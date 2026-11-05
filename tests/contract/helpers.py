"""Settings the contract suite reads from the environment (shared by conftest and the tests)."""

import os

DEFAULT_SAMPLE_DIR = "/sdcard/DCIM/Camera"
DEFAULT_ADB_PORT = 5037


def realphone_serial() -> str | None:
    """`SIFT_REALPHONE_SERIAL`: the phone's adb serial; the real-phone param skips without it."""
    return os.environ.get("SIFT_REALPHONE_SERIAL") or None


def realphone_adb_port() -> int:
    """`SIFT_REALPHONE_ADB_PORT`: the adb server's port (default 5037)."""
    return int(os.environ.get("SIFT_REALPHONE_ADB_PORT", str(DEFAULT_ADB_PORT)))


def realphone_sample_dir() -> str:
    """`SIFT_REALPHONE_SAMPLE_DIR`: the folder the walk, read and hash tests use on the phone."""
    return os.environ.get("SIFT_REALPHONE_SAMPLE_DIR", DEFAULT_SAMPLE_DIR)
