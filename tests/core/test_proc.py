import subprocess
import sys

import pytest

from sift.core.proc import subprocess_runner


def test_runs_and_captures_text():
    r = subprocess_runner(
        [sys.executable, "-c", "print('hi'); import sys; print('e', file=sys.stderr)"], 10
    )
    assert (r.returncode, r.stdout.strip(), r.stderr.strip()) == (0, "hi", "e")


def test_non_zero_exit_is_returned_not_raised():
    r = subprocess_runner([sys.executable, "-c", "raise SystemExit(3)"], 10)
    assert r.returncode == 3


def test_does_not_use_a_shell():
    r = subprocess_runner([sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo b"], 10)
    assert r.stdout.strip() == "a; echo b"


def test_timeout_raises():
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess_runner([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
