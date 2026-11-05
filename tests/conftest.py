from pathlib import Path

import pytest

from sift.core.paths import AppPaths


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SIFT_HOME at a tmp dir so no test touches the real ~/Library folders."""
    home = tmp_path / "sift-home"
    monkeypatch.setenv("SIFT_HOME", str(home))
    return home


@pytest.fixture
def paths(_isolate_home: Path) -> AppPaths:
    """AppPaths.default() under the isolated SIFT_HOME, with every folder created."""
    p = AppPaths.default()
    p.ensure()
    return p
