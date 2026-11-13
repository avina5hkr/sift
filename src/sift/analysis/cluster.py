"""Stage H, clusters of look-alike images for Teach the app (spec 8 H).

Cosine neighbours in chunks of 2,048 rows (numpy matrix multiply), edges at ≥ 0.88, union-find;
a cluster over 2,000 members is split again at 0.92; the medoid is the member with the highest
mean similarity; largest clusters first; singletons dropped. Near-duplicates inside a cluster
count once in its "unique" figure.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from sift.analysis.embed import from_blob
from sift.analysis.near import UnionFind
from sift.core.config import AnalysisConfig
from sift.core.db import Database

ALGO_VERSION = 1
CHUNK = 2048


@dataclass(frozen=True)
class Cluster:
    medoid: str
    members: tuple[str, ...]  # medoid first, then by similarity to it
    sims: tuple[float, ...]


def _components(hashes: Sequence[str], matrix: np.ndarray, threshold: float) -> list[list[int]]:
    uf = UnionFind()
    n = len(hashes)
    for start in range(0, n, CHUNK):
        block = matrix[start : start + CHUNK] @ matrix.T
        rows, cols = np.nonzero(block >= threshold)
        for r, c in zip(rows.tolist(), cols.tolist(), strict=True):
            i = start + r
            if i < c:
                uf.union(str(i), str(c))
    groups: dict[str, list[int]] = {}
    for key in list(uf.parent):
        groups.setdefault(uf.find(key), []).append(int(key))
    return [sorted(g) for g in groups.values() if len(g) > 1]


def find_clusters(hashes: Sequence[str], matrix: np.ndarray, cfg: AnalysisConfig) -> list[Cluster]:
    out: list[Cluster] = []
    for comp in _components(hashes, matrix, cfg.cluster_cosine):
        parts = [comp]
        if len(comp) > cfg.cluster_max_size:
            sub = _components([hashes[i] for i in comp], matrix[comp], cfg.cluster_split_cosine)
            parts = [[comp[j] for j in s] for s in sub]
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


def run_clusters(db: Database, cfg: AnalysisConfig, version: int) -> int:
    """Rebuild every cluster from the stored embeddings of this model version; returns the count."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT hash, embedding FROM content WHERE embedding IS NOT NULL AND embed_version = ? "
            "AND kind = 'image' ORDER BY hash",
            (version,),
        ).fetchall()
    hashes = [str(r[0]) for r in rows]
    clusters = find_clusters(hashes, np.stack([from_blob(r[1]) for r in rows]), cfg) if rows else []
    with db.tx() as conn:
        conn.execute("DELETE FROM cluster_member")
        conn.execute("DELETE FROM cluster")
        for c in clusters:
            cur = conn.execute(
                "INSERT INTO cluster (algo_version, size, medoid_hash) VALUES (?, ?, ?)",
                (ALGO_VERSION, len(c.members), c.medoid),
            )
            cid = int(cur.lastrowid or 0)
            conn.executemany(
                "INSERT INTO cluster_member (cluster_id, content_hash, sim) VALUES (?, ?, ?)",
                [(cid, h, s) for h, s in zip(c.members, c.sims, strict=True)],
            )
    return len(clusters)
