"""The two junk models (spec 9, Scoring), in numpy only (plan assumption E2).

`knn_vote`: the 15 nearest labelled items at cosine ≥ 0.80, weighted by cos⁴; junk_vote is the
junk share of the weight, support the junk neighbours at ≥ 0.85; protect_sim and keep_sim are
the highest cosines to protected- and keep-labelled items.
`LogReg`: L2 logistic regression with balanced class weights, C chosen by 5-fold
cross-validation on log loss, then calibrated on the out-of-fold scores (Platt sigmoid, or
isotonic with 200+ labels). junk_score = min(p_lr, junk_vote): both models must agree.
"""

from dataclasses import dataclass

import numpy as np

C_GRID = (0.01, 0.1, 1.0, 10.0)
FOLDS = 5
ISOTONIC_FROM = 200
CHUNK = 2048


@dataclass(frozen=True)
class Vote:
    junk_vote: np.ndarray
    support: np.ndarray
    protect_sim: np.ndarray
    keep_sim: np.ndarray


def knn_vote(
    queries: np.ndarray,
    labelled: np.ndarray,
    labels: np.ndarray,  # 1 junk, 0 keep, -1 protected
    *,
    k: int = 15,
    min_cosine: float = 0.80,
    support_cosine: float = 0.85,
    exclude_self: np.ndarray | None = None,  # per query, the labelled row that is itself, or -1
) -> Vote:
    n = len(queries)
    vote, support = np.zeros(n), np.zeros(n, dtype=np.int64)
    protect, keep = np.full(n, -1.0), np.full(n, -1.0)
    if n == 0 or len(labelled) == 0:
        return Vote(vote, support, protect, keep)
    is_junk, is_keep, is_prot = labels == 1, labels == 0, labels == -1
    kk = min(k, len(labelled))
    for start in range(0, n, CHUNK):
        sims = queries[start : start + CHUNK] @ labelled.T
        if exclude_self is not None:
            rows = np.nonzero(exclude_self[start : start + CHUNK] >= 0)[0]
            sims[rows, exclude_self[start : start + CHUNK][rows]] = -1.0
        if is_prot.any():
            protect[start : start + CHUNK] = sims[:, is_prot].max(axis=1)
        if is_keep.any():
            keep[start : start + CHUNK] = sims[:, is_keep].max(axis=1)
        top = np.argpartition(-sims, kk - 1, axis=1)[:, :kk]
        s = np.take_along_axis(sims, top, axis=1)
        use = s >= min_cosine
        w = np.where(use, np.clip(s, 0, 1) ** 4, 0.0)
        junk = is_junk[top]
        total = w.sum(axis=1)
        vote[start : start + CHUNK] = np.where(
            total > 0, (w * junk).sum(axis=1) / np.maximum(total, 1e-12), 0.0
        )
        support[start : start + CHUNK] = (junk & (s >= support_cosine)).sum(axis=1)
    return Vote(vote, support, protect, keep)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    out: np.ndarray = 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))
    return out


def _fit(x: np.ndarray, y: np.ndarray, c: float, sw: np.ndarray, iters: int = 30) -> np.ndarray:
    """Newton's method on Σ sw·logloss + ||w||² / (2C); the last weight is the unpenalised bias."""
    xb = np.hstack([x, np.ones((len(x), 1))])
    w = np.zeros(xb.shape[1])
    reg = np.full(xb.shape[1], 1.0 / c)
    reg[-1] = 0.0
    for _ in range(iters):
        p = _sigmoid(xb @ w)
        grad = xb.T @ (sw * (p - y)) + reg * w
        h = (xb * (sw * p * (1 - p))[:, None]).T @ xb + np.diag(reg + 1e-9)
        step = np.linalg.solve(h, grad)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def _decision(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    out: np.ndarray = x @ w[:-1] + w[-1]
    return out


def _balanced(y: np.ndarray) -> np.ndarray:
    pos, neg = max(int(y.sum()), 1), max(int(len(y) - y.sum()), 1)
    return np.where(y == 1, len(y) / (2 * pos), len(y) / (2 * neg))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def _folds(y: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Fold number per row, spreading each class evenly over the folds."""
    fold = np.zeros(len(y), dtype=np.int64)
    for cls in (0, 1):
        idx = rng.permutation(np.nonzero(y == cls)[0])
        fold[idx] = np.arange(len(idx)) % FOLDS
    return fold


def _isotonic(z: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pool-adjacent-violators: (x, value) points of a non-decreasing fit; each block gives
    its first and last score, and a score between points is interpolated linearly."""
    order = np.argsort(z)
    zs, ys = z[order], y[order].astype(float)
    vals: list[float] = []
    weights: list[float] = []
    starts: list[float] = []
    ends: list[float] = []
    for zi, yi in zip(zs, ys, strict=True):
        vals.append(float(yi))
        weights.append(1.0)
        starts.append(float(zi))
        ends.append(float(zi))
        while len(vals) > 1 and vals[-2] >= vals[-1]:
            w = weights[-2] + weights[-1]
            v = (vals[-2] * weights[-2] + vals[-1] * weights[-1]) / w
            vals[-2:], weights[-2:] = [v], [w]
            starts[-2:], ends[-2:] = [starts[-2]], [ends[-1]]
    xs = [x for s, e in zip(starts, ends, strict=True) for x in (s, e)]
    ys2 = [v for v in vals for _ in (0, 1)]
    return np.array(xs), np.array(ys2)


class LogReg:
    """Junk (1) against not junk (0), calibrated."""

    def __init__(self, x: np.ndarray, y: np.ndarray, seed: int = 0) -> None:
        self.mean = x.mean(axis=0)
        self.std = np.where(x.std(axis=0) > 1e-9, x.std(axis=0), 1.0)
        xs = (x - self.mean) / self.std
        rng = np.random.default_rng(seed)
        fold = _folds(y, rng)
        best: tuple[float, float, np.ndarray] | None = None
        for c in C_GRID:
            oof = np.zeros(len(y))
            for f in range(FOLDS):
                train, test = fold != f, fold == f
                if not test.any() or len(np.unique(y[train])) < 2:
                    continue
                w = _fit(xs[train], y[train], c, _balanced(y[train]))
                oof[test] = _decision(xs[test], w)
            loss = _logloss(y, _sigmoid(oof))
            if best is None or loss < best[0]:
                best = (loss, c, oof)
        assert best is not None
        _loss, self.c, oof = best
        self.w = _fit(xs, y, self.c, _balanced(y))
        self.isotonic = len(y) >= ISOTONIC_FROM
        if self.isotonic:
            self.steps = _isotonic(oof, y)
        else:  # Platt: a, b of sigmoid(a·z + b) fitted on the out-of-fold scores
            ab = _fit(oof[:, None], y.astype(float), 1e6, np.ones(len(y)))
            self.a, self.b = float(ab[0]), float(ab[1])

    def predict(self, x: np.ndarray) -> np.ndarray:
        z = _decision((x - self.mean) / self.std, self.w)
        if self.isotonic:
            xs, ys = self.steps
            return np.asarray(np.interp(z, xs, ys), dtype=float)
        return _sigmoid(self.a * z + self.b)
