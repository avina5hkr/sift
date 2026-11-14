"""Saving and loading the owner's teaching as a file (labels by content hash).

The file stays on the owner's Mac; it is never committed. A build made with SIFT_TEACHING bundles
it as `resources/teaching/starter.json`, which a fresh install loads once (`load_starter`).
Loading applies labels at once for pictures Sift knows, and keeps the rest in `label_import`
until a backup brings them in. A label already given in this app is never replaced.
"""

import json
import time
from typing import Any

from sift.cleanup.junk import bump_labels
from sift.core.db import Database

FORMAT = "sift-teaching"
VERSION = 1


def export(db: Database) -> dict[str, Any]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT content_hash, label, tag FROM label UNION "
            "SELECT content_hash, label, tag FROM label_import ORDER BY 1"
        ).fetchall()
    return {
        "format": FORMAT,
        "version": VERSION,
        "saved_at": int(time.time()),
        "labels": [{"hash": h, "label": lab, "tag": tag} for h, lab, tag in rows],
    }


def load(db: Database, data: dict[str, Any]) -> dict[str, int]:
    if data.get("format") != FORMAT or not isinstance(data.get("labels"), list):
        raise ValueError("This is not a Sift teaching file.")
    rows = []
    for item in data["labels"]:
        h, lab = str(item.get("hash", "")), str(item.get("label", ""))
        if (
            len(h) == 64
            and all(c in "0123456789abcdef" for c in h)
            and lab in ("junk", "keep", "protected")
        ):
            tag = item.get("tag")
            rows.append((h, lab, str(tag) if tag else None))
    now = int(time.time())
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO label_import (content_hash, label, tag, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (content_hash) DO UPDATE SET label = excluded.label, tag = excluded.tag",
            [(h, lab, tag, now) for h, lab, tag in rows],
        )
    applied = apply_pending(db)
    with db.read() as conn:
        waiting = int(conn.execute("SELECT count(*) FROM label_import").fetchone()[0])
    return {"read": len(rows), "applied": applied, "waiting": waiting}


def apply_pending(db: Database) -> int:
    """Turn loaded labels into real ones for content Sift now knows; returns how many."""
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO label (content_hash, label, tag, source, cluster_id, created_at) "
            "SELECT i.content_hash, i.label, i.tag, 'manual', NULL, i.created_at "
            "FROM label_import i "
            "JOIN content c ON c.hash = i.content_hash WHERE true "
            "ON CONFLICT (content_hash) DO NOTHING"
        )
        n = cur.rowcount or 0
        conn.execute("DELETE FROM label_import WHERE content_hash IN (SELECT hash FROM content)")
        if n:
            bump_labels(conn)
    return max(n, 0)


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1)


STARTER_DONE = "starter_teaching_loaded"


def starter_path() -> Any:
    from importlib.resources import files

    return files("sift.resources").joinpath("teaching", "starter.json")


def load_starter(db: Database, path: Any = None) -> dict[str, int] | None:
    """On a fresh install only (no labels yet, never loaded before), load the teaching bundled
    with this build. Returns what `load` did, or None when there was nothing to do."""
    path = path if path is not None else starter_path()
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    with db.read() as conn:
        done = conn.execute("SELECT 1 FROM setting WHERE key = ?", (STARTER_DONE,)).fetchone()
        labelled = conn.execute(
            "SELECT (SELECT count(*) FROM label) + (SELECT count(*) FROM label_import)"
        ).fetchone()[0]
    if done is not None or labelled:
        return None
    try:
        result = load(db, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        import logging

        logging.getLogger("sift.cleanup").warning("The bundled teaching could not be read: %s", e)
        result = None
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)",
            (STARTER_DONE, str(int(time.time()))),
        )
    return result
