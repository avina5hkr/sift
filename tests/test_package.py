import sys

import sift


def test_version():
    assert sift.__version__ == "0.1.0"


def test_runs_on_uv_managed_314():
    assert sys.version_info[:2] == (3, 14)
