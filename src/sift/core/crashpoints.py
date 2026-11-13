"""Named places where a test can make the process "crash" to prove recovery works.

Production code calls `hit("name")` at the moments that matter (between writing a file and
recording it, say). With no hook installed that costs one module-global lookup and a None
check. A test installs a hook that raises at the name it wants, and the code under test must
then recover on the next run.
"""

from collections.abc import Callable

_hook: Callable[[str], None] | None = None


def hit(name: str) -> None:
    if _hook is not None:
        _hook(name)


def install(hook: Callable[[str], None] | None) -> None:
    """Install `hook` (called with each name hit), or pass None to remove it. Tests only."""
    global _hook
    _hook = hook
