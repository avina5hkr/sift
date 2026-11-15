"""Storage locations: which data this Sift uses, read-only, and Reveal only for known places."""

import subprocess
from pathlib import Path

import pytest

from sift.core.context import AppContext
from tests.helpers import H, client_for


def test_the_places_this_sift_uses_and_the_sift_home_override(
    ctx: AppContext, tmp_path: Path
) -> None:
    ctx.config.backup.mac_root = str(tmp_path / "PhoneBackup")
    (tmp_path / "PhoneBackup").mkdir()
    body = client_for(ctx).get("/api/storage", headers=H).json()
    assert body["data_folder"]["path"] == str(ctx.paths.support)
    assert body["overridden"] is True  # the tests run with SIFT_HOME set
    assert body["database"]["path"].endswith("state.db") and body["database"]["exists"]
    assert body["settings_file"]["path"].endswith("config.toml")
    assert body["mac_backup"]["exists"] and body["mac_backup"]["free_bytes"] > 0
    assert body["ssd"] == {
        "configured": False,
        "connected": False,
        "name": None,
        "volume_id": None,
        "folder": None,
        "status": None,
    }


def test_reveal_opens_only_the_known_places(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: opened.append(args))
    client = client_for(ctx)
    assert (
        client.post("/api/storage/reveal", headers=H, json={"which": "database"}).status_code == 200
    )
    assert opened[-1][:2] == ["open", "-R"] and opened[-1][-1].endswith("state.db")
    assert client.post("/api/storage/reveal", headers=H, json={"which": "data"}).status_code == 200
    assert opened[-1] == ["open", "--", str(ctx.paths.support)]
    assert client.post("/api/storage/reveal", headers=H, json={"which": "/etc"}).status_code == 422
    ctx.config.backup.mac_root = str(tmp_path / "missing")
    assert client.post("/api/storage/reveal", headers=H, json={"which": "mac"}).status_code == 409
