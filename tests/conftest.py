from collections.abc import Iterator
from pathlib import Path

import pytest

from sift.core.db import Database
from sift.core.migrations import apply_migrations
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


@pytest.fixture
def db(paths: AppPaths) -> Iterator[Database]:
    """An opened, fully migrated Database under the isolated SIFT_HOME."""
    database = Database(paths.db)
    database.open()
    apply_migrations(database, paths.db_backups)
    yield database
    database.close()
