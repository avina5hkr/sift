import logging
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import settings

from sift.core.context import AppContext
from sift.core.db import Database
from sift.core.events import EventBus
from sift.core.logging import teardown_logging
from sift.core.migrations import apply_migrations
from sift.core.paths import AppPaths
from tests import helpers

# `dev` keeps local runs quick; `ci` runs 200 examples per property test with no deadline.
settings.register_profile("dev", max_examples=50)
settings.register_profile("ci", max_examples=200, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SIFT_HOME at a tmp dir so no test touches the real ~/Library folders."""
    home = tmp_path / "sift-home"
    monkeypatch.setenv("SIFT_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_real_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test (nor a `sift --no-window` subprocess it starts) may reach the real adb server.

    Tests that need the fake phone set SIFT_TRACKER=fake or pass --fake-phone explicitly.
    """
    monkeypatch.setenv("SIFT_TRACKER", "off")


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


@pytest.fixture
def anyio_backend() -> str:
    """Run every `@pytest.mark.anyio` test on asyncio only."""
    return "asyncio"


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture(autouse=True)
def restore_process_state() -> Iterator[None]:
    """Undo whatever a test installed process-wide: excepthooks and logging handlers/levels."""
    saved_excepthook = sys.excepthook
    saved_thread_excepthook = threading.excepthook
    names = ("", "sift")
    saved = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).level) for name in names
    }
    yield
    teardown_logging()
    sys.excepthook = saved_excepthook
    threading.excepthook = saved_thread_excepthook
    for name, (handlers, level) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.setLevel(level)


@pytest.fixture(autouse=True)
def _close_helper_resources() -> Iterator[None]:
    """Close every Database opened through tests/helpers.py once the test is done."""
    yield
    helpers.close_all()


@pytest.fixture
def ctx(paths: AppPaths) -> AppContext:
    """An AppContext over the tmp paths: migrated DB, token `t0k`, host 127.0.0.1:8765."""
    return helpers.make_ctx(paths)


@pytest.fixture
def client(ctx: AppContext) -> TestClient:
    return helpers.client_for(ctx)
