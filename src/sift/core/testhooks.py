"""The one gate for every test-only hook (`SIFT_TEST_*` environment variables).

Hooks work when running from source. A frozen (packaged) build ignores them unless
SIFT_ENABLE_TEST_HOOKS=1 is set, so a stray variable can never change what a real install does.
"""

import os
import sys

_PREFIX = "SIFT_TEST_"


def enabled() -> bool:
    """True when SIFT_ENABLE_TEST_HOOKS=1, or when not running from a frozen bundle."""
    if os.environ.get("SIFT_ENABLE_TEST_HOOKS") == "1":
        return True
    return not getattr(sys, "frozen", False) and not hasattr(sys, "_MEIPASS")


def flag(name: str) -> bool:
    """Whether the test hook `name` (a SIFT_TEST_* variable) is set to 1 and hooks are enabled."""
    if not name.startswith(_PREFIX):
        raise ValueError(f"{name} is not a test hook")
    return enabled() and os.environ.get(name) == "1"
