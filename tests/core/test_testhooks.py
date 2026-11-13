import sys

import pytest

from sift.core import testhooks


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIFT_ENABLE_TEST_HOOKS", raising=False)
    monkeypatch.delenv("SIFT_TEST_SLOW_JOB", raising=False)


def test_enabled_when_running_from_source() -> None:
    assert not getattr(sys, "frozen", False)
    assert testhooks.enabled()


def test_disabled_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not testhooks.enabled()


def test_disabled_in_a_pyinstaller_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", "/bundle", raising=False)
    assert not testhooks.enabled()


@pytest.mark.parametrize("value", ["0", "", "yes", "true"])
def test_only_exactly_one_enables_a_frozen_build(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("SIFT_ENABLE_TEST_HOOKS", value)
    assert not testhooks.enabled()
    monkeypatch.setenv("SIFT_ENABLE_TEST_HOOKS", "1")
    assert testhooks.enabled()


def test_flag_reads_the_hook_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not testhooks.flag("SIFT_TEST_SLOW_JOB")
    monkeypatch.setenv("SIFT_TEST_SLOW_JOB", "1")
    assert testhooks.flag("SIFT_TEST_SLOW_JOB")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not testhooks.flag("SIFT_TEST_SLOW_JOB")


def test_flag_only_takes_test_hook_names() -> None:
    with pytest.raises(ValueError):
        testhooks.flag("SIFT_HOME")
