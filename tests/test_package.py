import sys
import tomllib
from pathlib import Path

import sift


def test_version_comes_from_pyproject():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert sift.__version__ == project["project"]["version"]


def test_runs_on_uv_managed_314():
    assert sys.version_info[:2] == (3, 14)
