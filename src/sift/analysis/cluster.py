"""Stage H, clusters of look-alike images for Teach the app (spec 8 H).

Each image is joined to its 16 nearest neighbours at cosine ≥ 0.88 (numpy, rows in chunks of 512),
then union-find; a cluster over 2,000 members is split again at 0.92, then 0.94, 0.96, 0.98, and a
part still too big is cut into 2,000-member slices by similarity to its first member. The medoid
is the member with the highest mean similarity; largest clusters first; singletons dropped.
Cluster ids are never reused across rebuilds, so a label sent for an old card cannot land on a
new group (review M6 I1).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from sift.analysis.embed import from_blob
from sift.core.config import AnalysisConfig
from sift.core.db import Database

ALGO_VERSION = 2
CHUNK = 512
NEIGHBOURS = 16
SPLIT_STEPS = (0.02, 0.04, 0.06, 0.08)  # added to cluster_split_cosine - 0.02 → 0.92 … 0.98
NEXT_ID = "cluster_next_id"
BUILT_FOR = "clusters_built_for"


class Stopped(Exception):
    pass


@dataclass(frozen=True)
class Cluster:
    medoid: str
    members: tuple[str, ...]  # medoid first, then by similarity to it
    sims: tuple[float, ...]


def _components(
    matrix: np.ndarray, threshold: float, should_stop: Callable[[], bool]
) -> list[list[int]]:
    """Connected groups (indices into `matrix`) of the k-nearest-neighbour graph at `threshold`."""
    n = len(matrix)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    k = min(NEIGHBOURS + 1, n)
    for start in range(0, n, CHUNK):
        if should_stop():
            raise Stopped
        block = matrix[start : start + CHUNK] @ matrix.T
        top = np.argpartition(-block, k - 1, axis=1)[:, :k]
        scores = np.take_along_axis(block, top, axis=1)
        rows, cols = np.nonzero(scores >= threshold)
        for r, c in zip((rows + start).tolist(), top[rows, cols].tolist(), strict=True):
            if r != c:
                ra, rb = find(r), find(c)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def _split(
    matrix: np.ndarray, comp: list[int], cfg: AnalysisConfig, stop: Callable[[], bool]
) -> list[list[int]]:
    parts = [comp]
    base = cfg.cluster_split_cosine - SPLIT_STEPS[0]
    for step in SPLIT_STEPS:
        big = [p for p in parts if len(p) > cfg.cluster_max_size]
        if not big:
            return parts
        parts = [p for p in parts if len(p) <= cfg.cluster_max_size]
        for p in big:
            parts += [[p[j] for j in s] for s in _components(matrix[p], base + step, stop)]
    out = []
    for p in parts:
        if len(p) <= cfg.cluster_max_size:
            out.append(p)
            continue
        order = np.argsort(-(matrix[p] @ matrix[p[0]]))
        out += [
            [p[j] for j in order[i : i + cfg.cluster_max_size]]
            for i in range(0, len(p), cfg.cluster_max_size)
        ]
    return [p for p in out if len(p) > 1]


def find_clusters(
    hashes: Sequence[str],
    matrix: np.ndarray,
    cfg: AnalysisConfig,
    should_stop: Callable[[], bool] = lambda: False,
) -> list[Cluster]:
    out: list[Cluster] = []
    for comp in _components(matrix, cfg.cluster_cosine, should_stop):
        parts = (
            _split(matrix, comp, cfg, should_stop) if len(comp) > cfg.cluster_max_size else [comp]
        )
        for part in parts:
            m = matrix[part]
            sims = m @ m.T
            medoid = int(np.argmax(sims.mean(axis=1)))
            to_medoid = sims[medoid]
            order = np.argsort(-to_medoid)
            out.append(
                Cluster(
                    hashes[part[medoid]],
                    tuple(hashes[part[i]] for i in order),
                    tuple(float(to_medoid[i]) for i in order),
                )
            )
    out.sort(key=lambda c: -len(c.members))
    return out


def _setting(conn: object, key: str) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()  # type: ignore[attr-defined]
    return None if row is None else str(row[0])


def _put(conn: object, key: str, value: str) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def run_clusters(
    db: Database,
    cfg: AnalysisConfig,
    version: int,
    *,
    changed: bool = True,
    should_stop: Callable[[], bool] = lambda: False,
) -> int | None:
    """Rebuild every cluster from the stored embeddings of this model version; returns the count.
    Skipped (None) when nothing changed since the last build for this version, or when stopped;
    the earlier clusters then stay."""
    built_for = f"{version}:{ALGO_VERSION}"
    with db.read() as conn:
        if not changed and _setting(conn, BUILT_FOR) == built_for:
            return None
        rows = conn.execute(
            "SELECT hash, embedding FROM content WHERE embedding IS NOT NULL AND embed_version = ? "
            "AND kind = 'image' ORDER BY hash",
            (version,),
        ).fetchall()
    hashes = [str(r[0]) for r in rows]
    try:
        matrix = np.stack([from_blob(r[1]) for r in rows]) if rows else None
        clusters = find_clusters(hashes, matrix, cfg, should_stop) if matrix is not None else []
    except Stopped:
        return None
    with db.tx() as conn:
        used = conn.execute("SELECT IFNULL(max(id), 0) FROM cluster").fetchone()[0]
        next_id = max(int(_setting(conn, NEXT_ID) or 1), int(used) + 1)
        conn.execute("DELETE FROM cluster_member")
        conn.execute("DELETE FROM cluster")
        for c in clusters:
            conn.execute(
                "INSERT INTO cluster (id, algo_version, size, medoid_hash) VALUES (?, ?, ?, ?)",
                (next_id, ALGO_VERSION, len(c.members), c.medoid),
            )
            conn.executemany(
                "INSERT INTO cluster_member (cluster_id, content_hash, sim) VALUES (?, ?, ?)",
                [(next_id, h, s) for h, s in zip(c.members, c.sims, strict=True)],
            )
            next_id += 1
        _put(conn, NEXT_ID, str(next_id))
        _put(conn, BUILT_FOR, built_for)
    return len(clusters)
