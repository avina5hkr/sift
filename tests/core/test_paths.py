from pathlib import Path

import pytest

from sift.core.paths import AppPaths


def test_sift_home_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFT_HOME", str(tmp_path))
    p = AppPaths.default()
    assert p.db == tmp_path / "support" / "state.db"
    assert p.config == tmp_path / "support" / "config.toml"
    assert p.thumbs == tmp_path / "cache" / "thumbs"


def test_macos_locations_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIFT_HOME", raising=False)
    p = AppPaths.default()
    assert p.support == Path.home() / "Library/Application Support/Sift"
    assert p.cache == Path.home() / "Library/Caches/Sift"


def test_ensure_creates_all_dirs(tmp_path: Path) -> None:
    p = AppPaths(support=tmp_path / "s", cache=tmp_path / "c")
    p.ensure()
    for d in (p.support, p.logs, p.models, p.platform_tools, p.db_backups, p.thumbs):
        assert d.is_dir()


def test_derived_locations(tmp_path: Path) -> None:
    p = AppPaths(support=tmp_path / "s", cache=tmp_path / "c")
    assert p.lock == tmp_path / "s" / "sift.lock"
    assert p.db_backups == tmp_path / "s" / "backups"


def test_paths_fixture_matches_default(paths: AppPaths) -> None:
    assert paths == AppPaths.default()
    assert paths.support.is_dir()
    assert paths.thumbs.is_dir()
