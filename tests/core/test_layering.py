"""sift.core is the bottom layer: importing all of it must not pull in higher layers."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = """
import importlib, pkgutil, sys
import sift.core
for info in pkgutil.walk_packages(sift.core.__path__, "sift.core."):
    importlib.import_module(info.name)
bad = sorted(
    name for name in sys.modules
    if name.split(".")[:2] in (["sift", "server"], ["sift", "device"], ["sift", "backup"],
                               ["sift", "services"])
)
print(",".join(bad))
"""


def test_importing_every_core_module_never_imports_a_higher_layer() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""
