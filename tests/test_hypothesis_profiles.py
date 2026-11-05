import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = (
    "import tests.conftest\n"
    "from hypothesis import settings\n"
    "s = settings()\n"
    "print(s.max_examples, s.deadline is None)\n"
)


def _active_profile(profile: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "HYPOTHESIS_PROFILE"}
    if profile is not None:
        env["HYPOTHESIS_PROFILE"] = profile
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("profile", "max_examples", "deadline_disabled"),
    [(None, 50, False), ("dev", 50, False), ("ci", 200, True)],
)
def test_hypothesis_profile_follows_env(
    profile: str | None, max_examples: int, deadline_disabled: bool
) -> None:
    assert _active_profile(profile) == f"{max_examples} {deadline_disabled}"
