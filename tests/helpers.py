"""Shared test builders for the API: an app context and a TestClient over it."""

from contextlib import ExitStack
from pathlib import Path

from fastapi.testclient import TestClient

from sift import __version__
from sift.core.config import load_config
from sift.core.context import AppContext
from sift.core.db import Database
from sift.core.events import EventBus
from sift.core.migrations import apply_migrations
from sift.core.paths import AppPaths
from sift.server.app import create_app

TOKEN = "t0k"
HOST = "127.0.0.1:8765"
H = {"X-Sift-Token": TOKEN}

# Everything opened by make_ctx/make_client is closed by close_all(), which the autouse
# `_close_helper_resources` fixture in conftest.py calls after every test.
_cleanup = ExitStack()


def close_all() -> None:
    """Close every database opened by the helpers since the last call."""
    _cleanup.close()


def make_ctx(paths: AppPaths, web_dist: Path | None = None) -> AppContext:
    """A context over `paths`: config from `paths.config`, a migrated DB, token `t0k`."""
    config, warnings = load_config(paths.config)
    db = Database(paths.db)
    db.open()
    _cleanup.callback(db.close)
    apply_migrations(db, paths.db_backups)
    return AppContext(
        paths=paths,
        config=config,
        config_warnings=warnings,
        db=db,
        bus=EventBus(),
        token=TOKEN,
        allowed_hosts=frozenset({HOST}),
        version=__version__,
        web_dist=web_dist,
    )


def client_for(ctx: AppContext) -> TestClient:
    return TestClient(create_app(ctx), base_url=f"http://{HOST}")


def make_client(paths: AppPaths, web_dist: Path | None = None) -> TestClient:
    return client_for(make_ctx(paths, web_dist))
