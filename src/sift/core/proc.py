"""Run an external program: one place for argument-list-only subprocess calls."""

import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def subprocess_runner(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run `args` (no shell), capture text output, give up after `timeout` seconds.

    Raises `subprocess.TimeoutExpired` on timeout and `OSError` if the program cannot be
    started. A non-zero exit status is not an error here: the caller checks `returncode`.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
