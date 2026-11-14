"""K6 and review I1/I2: a built-in set added later joins the owner's config once."""

from pathlib import Path

from sift.app import _join_added_sets
from sift.core.config import Config, load_config, save_config
from sift.core.db import Database
from sift.core.migrations import apply_migrations

NAME = "WhatsApp chat backups"


def db_at(tmp_path: Path) -> Database:
    db = Database(tmp_path / "sift.db")
    db.open()
    apply_migrations(db, tmp_path / "db-backups")
    return db


def without_set() -> Config:
    cfg = Config()
    cfg.backup.sets = [s for s in cfg.backup.sets if s.name != NAME]
    return cfg


def test_joins_an_old_config_once_and_saves_it(tmp_path: Path) -> None:
    db, path = db_at(tmp_path), tmp_path / "config.toml"
    save_config(without_set(), path)
    cfg, warnings = load_config(path)
    _join_added_sets(cfg, db, path, warnings)
    assert NAME in [s.name for s in load_config(path)[0].backup.sets]
    cfg.backup.sets = [s for s in cfg.backup.sets if s.name != NAME]  # the owner removes it
    save_config(cfg, path)
    again, warnings = load_config(path)
    _join_added_sets(again, db, path, warnings)
    assert NAME not in [s.name for s in again.backup.sets]


def test_a_set_already_there_is_remembered_so_removing_it_sticks(tmp_path: Path) -> None:
    db, path = db_at(tmp_path), tmp_path / "config.toml"
    save_config(Config(), path)  # a fresh install already has the set
    cfg, warnings = load_config(path)
    _join_added_sets(cfg, db, path, warnings)
    cfg.backup.sets = [s for s in cfg.backup.sets if s.name != NAME]
    save_config(cfg, path)
    again, warnings = load_config(path)
    _join_added_sets(again, db, path, warnings)
    assert NAME not in [s.name for s in again.backup.sets]


def test_a_config_with_warnings_is_not_rewritten(tmp_path: Path) -> None:
    db, path = db_at(tmp_path), tmp_path / "config.toml"
    save_config(without_set(), path)
    text = path.read_text() + '\n[[backup.sets]]\nname = "Mine"\npaths = ["/etc/outside"]\n'
    path.write_text(text)
    cfg, warnings = load_config(path)
    assert warnings
    _join_added_sets(cfg, db, path, warnings)
    assert path.read_text() == text  # the owner's broken entry is still in the file
    assert NAME in [s.name for s in cfg.backup.sets]  # joined in memory for this run
