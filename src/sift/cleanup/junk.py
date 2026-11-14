"""Junk tiers, reasons and the Junk / Lower confidence tabs (spec 9 and 10).

`rebuild_junk` scores every image and video of one phone and writes `junk` (Suggested) and
`maybe` (Maybe) candidates. Tiers, first match wins: Protected, Keep, Suggested, Maybe, none.
Protected and Keep files are never written (I4, P5); neither is a file whose protection cannot
be judged yet (an image not read by OCR, a video of unknown length). Recordings are never
scored. The model trains on every labelled content with an embedding, on any phone, present or
not. One rebuild runs at a time; the tabs are rebuilt whenever any input changed (labels,
settings, word lists, phone files, analysis results), and pre-checking is allowed only for the
exact model a calibration passed on.
"""

import hashlib
import json
import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from sift.analysis.embed import from_blob
from sift.analysis.ocr import MIN_SHORT_SIDE
from sift.cleanup.classifier import LogReg, knn_vote
from sift.cleanup.lexicons import Lexicon, load, source_text
from sift.cleanup.near_tabs import selectable_ids
from sift.core.config import Config, phone_path_key
from sift.core.db import Database

LABELS_REV = "labels_rev"
TRAINED_REV = "scoring_trained_rev"
MODEL_VERSION = "scoring_model_version"
PRECHECK = "precheck_for"  # the model fingerprint a calibration passed on
T_SUGGEST = "t_suggest"
BUILT = "junk_built"
LONG_VIDEO_MS = 10 * 60 * 1000
SMALL_BYTES = 200_000
RECEIVED = ("whatsapp_received",)
_LABEL = {"junk": 1, "keep": 0, "protected": -1}
_LOCK = threading.Lock()


def get_setting(conn: Any, key: str) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def put_setting(conn: Any, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def bump_labels(conn: Any) -> None:
    """Call in the same transaction as any label write: the model is stale from now on."""
    put_setting(conn, LABELS_REV, str(int(get_setting(conn, LABELS_REV) or 0) + 1))


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


# Bump when the feature code or the classifier changes meaning: a calibration approved on the
# old pipeline must not carry over (external review, P2.6).
FEATURES_VERSION = 2
CLASSIFIER_VERSION = 2


def model_fingerprint(
    conn: Any, cfg: Config, embed_version: int, lexicons: Path | None = None
) -> str:
    """Everything the trained model, its predictions and its threshold depend on: the labels,
    the threshold, the embedding model and its version, the text reader's version, the feature
    and classifier code versions, the word lists as they read now, and the scoring settings."""
    from sift.analysis.ocr import VERSION as OCR_VERSION

    labelled = conn.execute(
        "SELECT count(*), IFNULL(sum(l.label = 'junk'), 0) FROM label l JOIN content c "
        "ON c.hash = l.content_hash WHERE c.embedding IS NOT NULL AND c.embed_version = ?",
        (embed_version,),
    ).fetchone()
    return _digest(
        get_setting(conn, LABELS_REV),
        get_setting(conn, T_SUGGEST),
        embed_version,
        cfg.analysis.embedding_model,
        OCR_VERSION,
        FEATURES_VERSION,
        CLASSIFIER_VERSION,
        [source_text(n, lexicons) for n in ("greeting", "document")],
        tuple(labelled),
        cfg.scoring.model_dump(mode="json"),
    )


def _inputs_fingerprint(
    conn: Any, device_id: int, cfg: Config, embed_version: int, lexicons: Path | None
) -> str:
    """Everything the junk tabs of this phone depend on (review M7 C2)."""
    files = conn.execute(
        "SELECT count(*), IFNULL(max(id), 0), IFNULL(sum(present), 0), IFNULL(sum(mtime), 0), "
        "IFNULL(sum(length(content_hash)), 0) FROM phone_file WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    content = conn.execute(
        "SELECT count(*), IFNULL(sum(ocr_version), 0), IFNULL(sum(embed_version), 0), "
        "IFNULL(sum(unreadable), 0), IFNULL(sum(is_screenshot), 0), "
        "IFNULL(sum(has_camera_exif), 0) FROM content WHERE kind IN ('image', 'video')"
    ).fetchone()
    groups = conn.execute(
        "SELECT (SELECT count(*) FROM near_member), (SELECT IFNULL(max(id), 0) FROM cluster)"
    ).fetchone()
    return _digest(
        device_id,
        model_fingerprint(conn, cfg, embed_version, lexicons),
        get_setting(conn, PRECHECK),
        cfg.cleanup.never_precheck,
        never_scored(cfg),
        [source_text(n, lexicons) for n in ("greeting", "document")],
        tuple(files),
        tuple(content),
        tuple(groups),
    )


@dataclass
class Item:
    hash: str
    kind: str
    size: int
    width: int | None
    height: int | None
    duration_ms: int | None
    camera_exif: bool
    screenshot: bool
    unreadable: bool
    text: str | None
    density: float | None
    embedding: np.ndarray | None
    paths: list[tuple[int, str, str, int]] = field(default_factory=list)  # id, path, origin, mtime
    copies: int = 0  # near-duplicate copies plus extra phone paths ("Seen N times")
    label: str | None = None
    junk_cluster: bool = False
    near_copies: int = 0
    ocr_done: bool = True
    listed: bool = True  # on this phone now (False: labelled, used for training only)


@dataclass(frozen=True)
class Verdict:
    tier: str  # protected | keep | suggested | maybe | none
    score: float | None
    support: int
    reasons: tuple[dict[str, str], ...]


_CONTENT = (
    "c.hash, c.kind, c.size, c.width, c.height, c.duration_ms, c.has_camera_exif, "
    "c.is_screenshot, c.unreadable, c.ocr_text, c.text_density, "
    "CASE WHEN c.embed_version = ? THEN c.embedding END, c.ocr_version"
)


def _items(db: Database, device_id: int, version: int, never: Sequence[str]) -> list[Item]:
    """This phone's present images and videos, then every other labelled content."""
    with db.read() as conn:
        files = conn.execute(
            f"SELECT p.id, p.remote_path, p.origin, p.mtime, {_CONTENT} "
            "FROM phone_file p JOIN content c ON c.hash = p.content_hash "
            "WHERE p.device_id = ? AND p.present = 1 AND c.kind IN ('image', 'video')",
            (version, device_id),
        ).fetchall()
        others = conn.execute(
            f"SELECT p.id, p.remote_path, p.origin, p.mtime, {_CONTENT} "
            "FROM label l JOIN content c ON c.hash = l.content_hash "
            "JOIN phone_file p ON p.content_hash = c.hash "
            "WHERE c.embed_version = ? AND c.embedding IS NOT NULL",
            (version, version),
        ).fetchall()
        near = dict(
            conn.execute(
                "SELECT m.content_hash, max(s.n) FROM near_member m JOIN (SELECT group_id, "
                "count(*) AS n FROM near_member GROUP BY group_id) s USING (group_id) "
                "GROUP BY m.content_hash"
            ).fetchall()
        )
        labels = dict(conn.execute("SELECT content_hash, label FROM label").fetchall())
        junk_clusters = {
            str(r[0])
            for r in conn.execute(
                "SELECT m.content_hash FROM cluster_member m JOIN (SELECT cm.cluster_id FROM "
                "cluster_member cm LEFT JOIN label l ON l.content_hash = cm.content_hash "
                "GROUP BY cm.cluster_id HAVING 2 * sum(l.label = 'junk') > count(*)) j "
                "USING (cluster_id)"
            )
        }
    out: dict[str, Item] = {}
    skipped: set[str] = set()  # content with a path in the Recordings set is never scored

    def add(row: Any, listed: bool) -> None:
        pid, path, origin, mtime, digest = (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            row[3],
            str(row[4]),
        )
        if listed and any(phone_path_key(path).startswith(n) for n in never):
            skipped.add(digest)
            return
        item = out.get(digest)
        if item is None:
            blob = row[15]
            w, h = row[7], row[8]
            item = Item(
                digest, str(row[5]), int(row[6]), w, h, row[9], bool(row[10]), bool(row[11]),
                bool(row[12]), row[13], row[14], from_blob(blob) if blob is not None else None,
            )  # fmt: skip
            item.ocr_done = (
                item.kind != "image"
                or int(row[16] or 0) >= 1
                or min(w or 0, h or 0) < MIN_SHORT_SIDE  # too small: OCR never reads it
            )
            item.label = labels.get(digest)
            item.junk_cluster = digest in junk_clusters
            item.listed = listed
            out[digest] = item
        elif not listed and item.listed:
            return  # a listed item keeps only this phone's present paths
        item.paths.append((pid, path, origin, int(mtime)))

    for row in files:
        add(row, True)
    for row in others:
        add(row, False)
    items = [i for h, i in out.items() if h not in skipped]
    for i in items:
        i.near_copies = max(int(near.get(i.hash, 1)) - 1, 0)
        i.copies = i.near_copies + len(i.paths) - 1
    return items


def _features(items: Sequence[Item], greet: Lexicon, doc: Lexicon, now: float) -> np.ndarray:
    rows = []
    for i in items:
        origins = {p[2] for p in i.paths}
        pixels = (i.width or 0) * (i.height or 0)
        age = max(now - min(p[3] for p in i.paths), 0) / 86400
        rows.append(
            [
                math.log1p(i.size),
                math.log1p(pixels),
                (i.width or 1) / (i.height or 1),
                i.density or 0.0,
                (i.text or "").count("\n") + (1 if i.text else 0),
                float(greet.hit(i.text) is not None),
                float(doc.hit(i.text) is not None),
                float("whatsapp_received" in origins),
                float("whatsapp_sent" in origins),
                float("whatsapp_private" in origins),
                float(i.camera_exif),
                float(i.screenshot),
                math.log1p(i.near_copies),
                float(len(i.paths)),
                math.log1p(age),
            ]
        )
    return np.asarray(rows, dtype=np.float64).reshape(len(items), 15)


@dataclass
class Model:
    version: int
    logreg: LogReg | None  # None in cold start
    labelled: np.ndarray
    labels: np.ndarray
    hashes: list[str]


def train(items: Sequence[Item], feats: np.ndarray, cfg: Config, version: int) -> Model:
    idx = [n for n, i in enumerate(items) if i.label in _LABEL and i.embedding is not None]
    labels = np.array([_LABEL[str(items[n].label)] for n in idx], dtype=np.int64)
    emb = np.stack([items[n].embedding for n in idx]) if idx else np.zeros((0, 1))  # type: ignore[misc]
    junk, keep = int((labels == 1).sum()), int((labels == 0).sum())
    lr = None
    if junk >= cfg.scoring.min_labels_each and keep >= cfg.scoring.min_labels_each:
        x = np.hstack([emb, feats[idx]])
        lr = LogReg(x, (labels == 1).astype(np.float64))
    return Model(version, lr, emb.astype(np.float32), labels, [items[n].hash for n in idx])


def _reasons(i: Item, greet_hit: str | None, support: int) -> tuple[dict[str, str], ...]:
    out = []
    if i.label == "junk":
        out.append({"code": "labelled", "text": "You marked this junk"})
    if support:
        out.append({"code": "like_junk", "text": f"Looks like {support} images you marked junk"})
    if greet_hit:
        out.append({"code": "text", "text": f"Text: {greet_hit}"})
    if i.copies >= 1:
        out.append({"code": "seen", "text": f"Seen {i.copies + 1} times"})
    if i.size < SMALL_BYTES and not i.camera_exif:
        out.append({"code": "small", "text": f"{max(i.size // 1000, 1)} KB, no camera data"})
    if any(p[2] in RECEIVED for p in i.paths):
        out.append({"code": "received", "text": "WhatsApp received"})
    return tuple(out[:3])


def is_protected(i: Item, cfg: Config, doc: Lexicon, protect_sim: float) -> bool:
    s = cfg.scoring
    origins = {p[2] for p in i.paths}
    folders = tuple(phone_path_key(f).rstrip("/") + "/" for f in s.protected_folders)
    return bool(
        i.camera_exif
        or "camera" in origins
        or (s.protect_screenshots and (i.screenshot or "screenshot" in origins))
        or doc.hit(i.text) is not None
        or i.label == "protected"
        or protect_sim >= s.protect_cosine
        or i.unreadable
        or (folders and any(phone_path_key(p[1]).startswith(folders) for p in i.paths))
        or (
            i.kind == "video"
            and (i.duration_ms is None or i.duration_ms > LONG_VIDEO_MS)  # unknown: protect
            and not i.junk_cluster
        )
    )


def verdicts(
    items: Sequence[Item],
    feats: np.ndarray,
    model: Model,
    cfg: Config,
    *,
    t_suggest: float,
    greet: Lexicon,
    doc: Lexicon,
) -> list[Verdict]:
    s = cfg.scoring
    emb_rows = [n for n, i in enumerate(items) if i.embedding is not None]
    pos = {h: n for n, h in enumerate(model.hashes)}
    vote = None
    if emb_rows and len(model.labels):
        q = np.stack([items[n].embedding for n in emb_rows]).astype(np.float32)  # type: ignore[misc]
        self_idx = np.array([pos.get(items[n].hash, -1) for n in emb_rows])
        vote = knn_vote(
            q, model.labelled, model.labels, k=s.knn_k, min_cosine=s.knn_min_cosine,
            exclude_self=self_idx,
        )  # fmt: skip
    p_lr = np.zeros(len(items))
    if model.logreg is not None and emb_rows:
        x = np.hstack([np.stack([items[n].embedding for n in emb_rows]), feats[emb_rows]])  # type: ignore[misc]
        p_lr[emb_rows] = model.logreg.predict(x)
    row_of = {n: r for r, n in enumerate(emb_rows)}
    out = []
    for n, i in enumerate(items):
        r = row_of.get(n)
        jv = float(vote.junk_vote[r]) if vote is not None and r is not None else 0.0
        sup = int(vote.support[r]) if vote is not None and r is not None else 0
        psim = float(vote.protect_sim[r]) if vote is not None and r is not None else -1.0
        ksim = float(vote.keep_sim[r]) if vote is not None and r is not None else -1.0
        if is_protected(i, cfg, doc, psim):
            out.append(Verdict("protected", None, 0, ()))
            continue
        if i.label == "keep" or ksim >= s.keep_cosine:
            out.append(Verdict("keep", None, 0, ()))
            continue
        if not i.ocr_done:  # the document words are not checked yet: not listed
            out.append(Verdict("none", None, 0, ()))
            continue
        g = greet.hit(i.text)
        score = min(float(p_lr[n]), jv) if model.logreg is not None and r is not None else None
        reasons = _reasons(i, g, sup)
        if i.label == "junk":
            out.append(Verdict("suggested", score, sup, reasons))
            continue
        if model.logreg is not None and r is None:  # no embedding yet: no score
            out.append(Verdict("none", None, 0, ()))
            continue
        if score is None:  # cold start: lexicon hits with 2+ copies only under Maybe
            tier = "maybe" if g is not None and i.copies >= 2 else "none"
            out.append(Verdict(tier, None, sup, reasons if tier == "maybe" else ()))
            continue
        if i.kind == "video":  # E7: a near-duplicate copy or a junk cluster, on top
            t = max(s.t_suggest_video, t_suggest)
            ok = (i.near_copies >= 1 or i.junk_cluster) and (
                g is not None or i.copies >= 2 or i.junk_cluster
            )
        else:
            t = t_suggest
            ok = g is not None or i.copies >= 2 or i.junk_cluster
        if score >= t and sup >= s.min_support and ok:
            out.append(Verdict("suggested", score, sup, reasons))
        elif score >= s.t_maybe:
            out.append(Verdict("maybe", score, sup, reasons))
        else:
            out.append(Verdict("none", score, sup, ()))
    return out


def never_scored(cfg: Config) -> list[str]:
    """Path prefixes (keys) of the Recordings set: recordings are never classified as junk."""
    return sorted(
        phone_path_key(p).rstrip("/") + "/"
        for bs in cfg.backup.sets
        if bs.name == "Recordings"
        for p in bs.paths
    )


def state(
    db: Database, cfg: Config, embed_version: int, lexicons: Path | None = None
) -> dict[str, Any]:
    with db.read() as conn:
        return state_in(conn, cfg, embed_version, lexicons)


def state_in(
    conn: Any, cfg: Config, embed_version: int, lexicons: Path | None = None
) -> dict[str, Any]:
    t = get_setting(conn, T_SUGGEST)
    fp = model_fingerprint(conn, cfg, embed_version, lexicons)
    return {
        "labels_rev": int(get_setting(conn, LABELS_REV) or 0),
        "trained_rev": int(get_setting(conn, TRAINED_REV) or -1),
        "model_version": int(get_setting(conn, MODEL_VERSION) or 0),
        "fingerprint": fp,
        "precheck_allowed": get_setting(conn, PRECHECK) == fp,
        "t_suggest": float(t) if t is not None else cfg.scoring.t_suggest,
    }


def label_counts(db: Database, embed_version: int) -> dict[str, int]:
    """Labels the model can learn from (the content has an embedding of this version)."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT l.label, count(*) FROM label l JOIN content c ON c.hash = l.content_hash "
            "WHERE c.embedding IS NOT NULL AND c.embed_version = ? GROUP BY l.label",
            (embed_version,),
        ).fetchall()
    return {str(k): int(v) for k, v in rows}


_cache_lock = threading.Lock()
_cache: dict[tuple[str, int], Model] = {}


def _trained(items: Sequence[Item], feats: np.ndarray, cfg: Config, version: int, fp: str) -> Model:
    """The model for these inputs (this phone's files and features, labels, threshold,
    embeddings, word lists, settings): trained once and reused until any of them change."""
    key = (fp, version)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    model = train(items, feats, cfg, version)
    with _cache_lock:
        _cache.clear()  # one model at a time
        _cache[key] = model
    return model


def score_all(
    db: Database, device_id: int, cfg: Config, embed_version: int, lexicons: Path | None
) -> tuple[list[Item], list[Verdict], Model, dict[str, Any]]:
    """Score every image and video of this phone, retraining first when labels changed.
    Returns the listed items, their verdicts, the model and the state it was built from."""
    st = state(db, cfg, embed_version, lexicons)
    greet, doc = load("greeting", lexicons), load("document", lexicons)
    every = _items(db, device_id, embed_version, never_scored(cfg))
    feats = _features(every, greet, doc, time.time())
    version = st["model_version"]
    if st["trained_rev"] != st["labels_rev"]:
        version += 1
        with db.tx() as conn:
            put_setting(conn, MODEL_VERSION, str(version))
            put_setting(conn, TRAINED_REV, str(st["labels_rev"]))
    with db.read() as conn:  # this phone, its features, the word lists and the model inputs
        key = _inputs_fingerprint(conn, device_id, cfg, embed_version, lexicons)
    model = _trained(every, feats, cfg, version, key)
    found = verdicts(every, feats, model, cfg, t_suggest=st["t_suggest"], greet=greet, doc=doc)
    keep = [n for n, i in enumerate(every) if i.listed]
    return [every[n] for n in keep], [found[n] for n in keep], model, st


def rebuild_junk(
    db: Database, device_id: int, cfg: Config, embed_version: int, lexicons: Path | None = None
) -> dict[str, int]:
    """Rewrite this phone's junk and maybe candidates; returns counts per tier. One at a time;
    the stored key is taken before reading, so a change made meanwhile triggers another."""
    with _LOCK:
        with db.read() as conn:
            key = _inputs_fingerprint(conn, device_id, cfg, embed_version, lexicons)
        items, found, model, st = score_all(db, device_id, cfg, embed_version, lexicons)
        precheck = st["precheck_allowed"] and not cfg.cleanup.never_precheck
        rows = []
        junk_ids: set[int] = set()
        counts = {"suggested": 0, "maybe": 0, "protected": 0, "keep": 0, "none": 0}
        for n, (item, v) in enumerate(zip(items, found, strict=True), 1):
            counts[v.tier] += 1
            if v.tier not in ("suggested", "maybe"):
                continue
            tab = "junk" if v.tier == "suggested" else "maybe"
            reasons = json.dumps(list(v.reasons))
            checked = int(precheck and v.tier == "suggested")
            for pid, _p, _o, _m in item.paths:
                rows.append(
                    (
                        pid,
                        tab,
                        v.tier,
                        v.score,
                        v.support,
                        reasons,
                        n,
                        "item",
                        checked,
                        model.version,
                    )
                )
                if tab == "junk":
                    junk_ids.add(pid)
        keep_ticks = set(selectable_ids(db, device_id, "near"))
        keep_ticks |= set(selectable_ids(db, device_id, "similar"))
        with db.tx() as conn:
            before = [
                int(r[0])
                for r in conn.execute(
                    "SELECT c.phone_file_id FROM candidate c JOIN phone_file p "
                    "ON p.id = c.phone_file_id "
                    "WHERE c.tab IN ('junk', 'maybe') AND p.device_id = ?",
                    (device_id,),
                )
            ]
            keep_ticks |= {
                int(r[0])
                for r in conn.execute("SELECT phone_file_id FROM candidate WHERE tab = 'exact'")
            }
            conn.execute(
                "DELETE FROM candidate WHERE tab IN ('junk', 'maybe') AND phone_file_id IN "
                "(SELECT id FROM phone_file WHERE device_id = ?)",
                (device_id,),
            )
            conn.executemany(
                "INSERT INTO candidate (phone_file_id, tab, tier, junk_score, support, reasons, "
                "group_id, role, default_checked, model_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            # a tick on a file that left the Junk tab (Not junk, protected now, or only Maybe,
            # which is never ticked) is dropped unless another tab still offers that file
            dropped = [(pid,) for pid in before if pid not in junk_ids and pid not in keep_ticks]
            conn.executemany("DELETE FROM selection WHERE phone_file_id = ?", dropped)
            put_setting(conn, BUILT, key)
        return counts


def ensure_fresh(
    db: Database, device_id: int, cfg: Config, embed_version: int, lexicons: Path | None = None
) -> bool:
    """Rebuild the junk tabs when any of their inputs changed."""
    with db.read() as conn:
        key = _inputs_fingerprint(conn, device_id, cfg, embed_version, lexicons)
        if get_setting(conn, BUILT) == key:
            return False
    rebuild_junk(db, device_id, cfg, embed_version, lexicons)
    return True
