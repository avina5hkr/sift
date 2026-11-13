"""Calibration, the precision gate (spec 9).

`start` draws a random sample of up to 300 Suggested items (model suggestions, not files the
owner labelled junk). The owner marks each Correct or Wrong. When the last one is marked:
zero wrong stores a passed calibration row and allows pre-checking; any wrong item becomes keep
(source calibration), T_suggest rises to just above the highest wrong score (+ 0.01), the
model retrains, and a fresh sample is needed.
"""

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sift.cleanup.junk import PRECHECK, T_SUGGEST, bump_labels, put_setting, score_all, state
from sift.core.config import Config
from sift.core.db import Database


class NothingToCalibrate(Exception):
    """There are no Suggested items to sample."""


@dataclass(frozen=True)
class Result:
    sample_size: int
    errors: int
    passed: bool
    threshold: float


def start(
    db: Database,
    device_id: int,
    cfg: Config,
    embed_version: int,
    lexicons: Path | None = None,
    rng: random.Random | None = None,
) -> int:
    """Replace the current sample with a fresh one; returns its size."""
    items, found, model = score_all(db, device_id, cfg, embed_version, lexicons)
    pool = [
        (i.hash, i.paths[0][0], v.score)
        for i, v in zip(items, found, strict=True)
        if v.tier == "suggested" and i.label != "junk" and v.score is not None
    ]
    if not pool:
        raise NothingToCalibrate("There is nothing suggested to check yet.")
    sample = (rng or random.SystemRandom()).sample(
        pool, min(cfg.scoring.calibration_sample, len(pool))
    )
    with db.tx() as conn:
        conn.execute("DELETE FROM calibration_item")
        conn.executemany(
            "INSERT INTO calibration_item (content_hash, phone_file_id, score, verdict, "
            "model_version) VALUES (?, ?, ?, NULL, ?)",
            [(h, pid, s, model.version) for h, pid, s in sample],
        )
    return len(sample)


def items(db: Database) -> list[tuple[str, int, float, int | None]]:
    with db.read() as conn:
        return [
            (str(h), int(p), float(s), None if v is None else int(v))
            for h, p, s, v in conn.execute(
                "SELECT content_hash, phone_file_id, score, verdict FROM calibration_item "
                "ORDER BY rowid"
            )
        ]


def mark(db: Database, cfg: Config, content_hash: str, correct: bool) -> Result | None:
    """Record one verdict; when it was the last, finish and return the result."""
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE calibration_item SET verdict = ? WHERE content_hash = ?",
            (int(correct), content_hash),
        )
        if cur.rowcount == 0:
            raise KeyError(content_hash)
        left = conn.execute(
            "SELECT count(*) FROM calibration_item WHERE verdict IS NULL"
        ).fetchone()[0]
    return _finish(db, cfg) if left == 0 else None


def _finish(db: Database, cfg: Config) -> Result:
    st = state(db, cfg)
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT content_hash, score, verdict, model_version FROM calibration_item"
        ).fetchall()
        wrong = [(str(h), float(s)) for h, s, v, _m in rows if v == 0]
        same_model = all(int(m) == st["model_version"] for *_x, m in rows)
        passed = not wrong and same_model
        threshold = st["t_suggest"]
        if wrong:
            threshold = min(max(s for _h, s in wrong) + 0.01, 1.0)
            conn.executemany(
                "INSERT INTO label (content_hash, label, tag, source, cluster_id, created_at) "
                "VALUES (?, 'keep', NULL, 'calibration', NULL, ?) ON CONFLICT (content_hash) DO "
                "UPDATE SET label = 'keep', source = 'calibration', "
                "created_at = excluded.created_at "
                "WHERE label.label != 'protected'",
                [(h, int(time.time())) for h, _s in wrong],
            )
            bump_labels(conn)
            put_setting(conn, T_SUGGEST, repr(threshold))
        put_setting(conn, PRECHECK, "1" if passed else "0")
        conn.execute(
            "INSERT INTO calibration (created_at, sample_size, errors, threshold, model_version, "
            "passed) VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), len(rows), len(wrong), threshold, st["model_version"], int(passed)),
        )
        conn.execute("DELETE FROM calibration_item")
    return Result(len(rows), len(wrong), passed, threshold)


def last(db: Database) -> dict[str, Any] | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT created_at, sample_size, errors, threshold, model_version, passed "
            "FROM calibration ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    keys = ("created_at", "sample_size", "errors", "threshold", "model_version", "passed")
    out = dict(zip(keys, row, strict=True))
    out["passed"] = bool(out["passed"])
    return out
