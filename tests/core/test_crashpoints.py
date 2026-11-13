import timeit

import pytest

from sift.core import crashpoints


def test_hit_is_a_no_op_without_a_hook() -> None:
    crashpoints.hit("anything")  # nothing installed: nothing happens


def test_installed_hook_sees_every_hit_and_can_raise() -> None:
    seen: list[str] = []

    def hook(name: str) -> None:
        seen.append(name)
        if name == "boom":
            raise RuntimeError("crash here")

    crashpoints.install(hook)
    crashpoints.hit("a")
    with pytest.raises(RuntimeError, match="crash here"):
        crashpoints.hit("boom")
    assert seen == ["a", "boom"]


def test_install_none_removes_the_hook() -> None:
    seen: list[str] = []
    crashpoints.install(seen.append)
    crashpoints.install(None)
    crashpoints.hit("a")
    assert seen == []


def test_the_test_fixture_clears_hooks_between_tests() -> None:
    seen: list[str] = []
    crashpoints.install(seen.append)  # the autouse fixture must undo this after the test


def test_previous_test_left_no_hook() -> None:
    assert crashpoints._hook is None


def test_hit_without_a_hook_is_cheap() -> None:
    per_call = timeit.timeit(lambda: crashpoints.hit("x"), number=200_000) / 200_000
    assert per_call < 2e-6
