"""The owner's teaching as a file: their junk / keep / protected labels, by content hash.

What it is: a list of decisions about specific pictures (SHA-256 of the file), with where each
came from (a picture's own label, or one label given to a whole group). What it is not: a
trained model or picture features. It helps only where the same pictures exist again (a new
install on the same Mac, the same phone): loading applies labels at once for pictures Sift
knows and keeps the rest in `label_import` until a backup brings them in. A label already given
in this app is never replaced.

Format 2 keeps provenance; format 1 files (labels only) still load, every label as a picture's
own. A newer format is refused. The file stays on the owner's Mac and is never committed; a build
bundles one only when SIFT_TEACHING names it (`load_starter` loads it once on a fresh install).
"""

import hashlib
import json
import time
from typing import Any

from sift.cleanup.junk import bump_labels
from sift.core.db import Database

FORMAT = "sift-teaching"
VERSION = 2
SOURCES = ("manual", "cluster", "feedback", "calibration")


def export(db: Database) -> dict[str, Any]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT content_hash, label, tag, source, CASE WHEN source = 'cluster' "
            "THEN 'g' || cluster_id END FROM label UNION "
            "SELECT content_hash, label, tag, IFNULL(source, 'manual'), group_key "
            "FROM label_import ORDER BY 1"
        ).fetchall()
    return {
        "format": FORMAT,
        "version": VERSION,
        "saved_at": int(time.time()),
        "about": "Labels by content hash (SHA-256 of each file), not a trained model.",
        "labels": [
            {"hash": h, "label": lab, "tag": tag, "source": src, "group": group}
            for h, lab, tag, src, group in rows
        ],
    }


def load(db: Database, data: dict[str, Any]) -> dict[str, int]:
    if data.get("format") != FORMAT or not isinstance(data.get("labels"), list):
        raise ValueError("This is not a Sift teaching file.")
    version = data.get("version", 1)
    if not isinstance(version, int) or not 1 <= version <= VERSION:
        raise ValueError("This teaching file is from a newer Sift; update Sift to load it.")
    # a group key is scoped to the file it came from, so two files never merge their groups
    scope = hashlib.sha256(json.dumps(data.get("saved_at")).encode()).hexdigest()[:8]
    rows = []
    for item in data["labels"]:
        if not isinstance(item, dict):
            continue
        h, lab = str(item.get("hash", "")), str(item.get("label", ""))
        if not (
            len(h) == 64
            and all(c in "0123456789abcdef" for c in h)
            and lab in ("junk", "keep", "protected")
        ):
            continue
        tag = item.get("tag")
        src = str(item.get("source") or "manual") if version >= 2 else "manual"
        src = src if src in SOURCES else "manual"
        group = item.get("group") if src == "cluster" else None
        key = f"{scope}:{group}" if group else None
        rows.append((h, lab, str(tag) if tag else None, "cluster" if key else src, key))
    now = int(time.time())
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO label_import (content_hash, label, tag, source, group_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (content_hash) DO UPDATE SET "
            "label = excluded.label, tag = excluded.tag, source = excluded.source, "
            "group_key = excluded.group_key",
            [(h, lab, tag, src, key, now) for h, lab, tag, src, key in rows],
        )
    applied = apply_pending(db)
    with db.read() as conn:
        waiting = int(conn.execute("SELECT count(*) FROM label_import").fetchone()[0])
    return {"read": len(rows), "applied": applied, "waiting": waiting}


def _group_id(key: str) -> int:
    """A loaded group's id: negative, so it can never be a real cluster's id."""
    return -(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16) + 1)


def apply_pending(db: Database) -> int:
    """Turn loaded labels into real ones for content Sift now knows; returns how many. A group
    label stays a group label (source cluster, its own negative group id)."""
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT i.content_hash, i.label, i.tag, IFNULL(i.source, 'manual'), i.group_key, "
            "i.created_at FROM label_import i JOIN content c ON c.hash = i.content_hash"
        ).fetchall()
        n = 0
        for h, lab, tag, src, key, created in rows:
            cur = conn.execute(
                "INSERT INTO label (content_hash, label, tag, source, cluster_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (content_hash) DO NOTHING",
                (h, lab, tag, src if src in SOURCES else "manual",
                 _group_id(key) if key else None, created),
            )  # fmt: skip
            n += max(cur.rowcount or 0, 0)
        conn.execute("DELETE FROM label_import WHERE content_hash IN (SELECT hash FROM content)")
        if n:
            bump_labels(conn)
    return n


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
