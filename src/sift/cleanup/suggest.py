"""What Sift suggests for a Teach-the-app group before (and while) the owner teaches it.

Built in (no teaching needed, nothing personal shipped): the group's medoid picture is compared
with short CLIP text descriptions of typical junk (greeting cards, quotes, memes, forwards) and of
typical keepers (people, documents, places). The owner's teaching then moves that guess: the
medoid's nearest marked pictures (outside the group) vote, and their vote counts for more as
labels grow (20 labels: half the say) and as they get closer. One blended score decides.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from sift.analysis.embed import EmbeddingModel, from_blob
from sift.cleanup.classifier import knn_vote
from sift.core.db import Database

JUNK_PROMPTS = {
    "a good morning greeting card with flowers": "a greeting card",
    "a good night wishes image with text": "a greeting card",
    "a festival greeting card with wishes": "a festival greeting",
    "a motivational quote written on a colourful background": "a quote picture",
    "an internet meme with a caption": "a meme",
    "a forwarded religious picture of a god with text": "a devotional forward",
    "a funny joke picture with text": "a joke picture",
    "a birthday wishes greeting image": "a greeting card",
}
KEEP_PROMPTS = {
    "a family photo of people together": "people",
    "a selfie of a person": "a selfie",
    "a photo of a child": "a child",
    "a photo of an identity document, bill or receipt": "a document",
    "a group photo at a wedding or party": "an event",
    "a travel photo of a place or landscape": "a place",
    "a photo of a pet": "a pet",
    "a photo of handwritten notes": "notes",
}
LOGIT_SCALE = 100.0  # CLIP's own temperature
SURE = 0.75
MIN_LABELS = 3

_prompts: dict[int, tuple[np.ndarray, np.ndarray]] = {}


@dataclass(frozen=True)
class Suggestion:
    label: str | None  # junk, keep, or None when unsure
    reason: str
    source: str  # "built-in" or "your teaching"


def _prompt_vectors(model: EmbeddingModel) -> tuple[np.ndarray, np.ndarray]:
    cached = _prompts.get(model.version)
    if cached is None:
        cached = (model.encode_text(list(JUNK_PROMPTS)), model.encode_text(list(KEEP_PROMPTS)))
        _prompts[model.version] = cached
    return cached


def _built_in_score(model: EmbeddingModel, vec: np.ndarray) -> tuple[float, str, str]:
    """(probability of junk, what it looks like if junk, what it looks like if keep)."""
    junk, keep = _prompt_vectors(model)
    js, ks = junk @ vec, keep @ vec
    p_junk = float(1.0 / (1.0 + np.exp(-LOGIT_SCALE * (float(js.max()) - float(ks.max())))))
    return (
        p_junk,
        list(JUNK_PROMPTS.values())[int(js.argmax())],
        list(KEEP_PROMPTS.values())[int(ks.argmax())],
    )


def built_in(model: EmbeddingModel, vec: np.ndarray) -> Suggestion:
    return suggest(model, vec, None, ())


@dataclass
class Teaching:
    vectors: np.ndarray
    labels: np.ndarray  # 1 junk, 0 keep, -1 protected
    hashes: list[str]


def load_teaching(db: Database, version: int) -> Teaching | None:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT l.content_hash, l.label, c.embedding FROM label l JOIN content c "
            "ON c.hash = l.content_hash WHERE c.embedding IS NOT NULL AND c.embed_version = ?",
            (version,),
        ).fetchall()
    code = {"junk": 1, "keep": 0, "protected": -1}
    labels = np.array([code[str(r[1])] for r in rows], dtype=np.int64)
    if int((labels == 1).sum()) < MIN_LABELS or int((labels == 0).sum()) < MIN_LABELS:
        return None
    return Teaching(
        np.stack([from_blob(r[2]) for r in rows]).astype(np.float32),
        labels,
        [str(r[0]) for r in rows],
    )


def _teaching_score(
    t: Teaching, vec: np.ndarray, members: Sequence[str]
) -> tuple[float, float, int]:
    """(junk vote, how much it counts 0..1, junk neighbours) from the nearest marked pictures
    outside this group. It counts for more with more labels and with closer neighbours."""
    inside = set(members)
    keep = np.array([h not in inside for h in t.hashes])
    if not keep.any():
        return 0.5, 0.0, 0
    vote = knn_vote(vec[None, :].astype(np.float32), t.vectors[keep], t.labels[keep], k=15)
    jv, support = float(vote.junk_vote[0]), int(vote.support[0])
    nearest = max(float(vote.keep_sim[0]), float(vote.protect_sim[0]))
    if jv > 0 or support:
        nearest = max(nearest, 0.85)
    if jv == 0.0 and nearest < 0.80:
        return 0.5, 0.0, 0  # nothing taught is close to this group
    amount = len(t.labels) / (len(t.labels) + 20.0)  # 20 labels: half the say
    closeness = min(max((nearest - 0.75) / 0.15, 0.0), 1.0)
    return jv, amount * closeness, support


def suggest(
    model: EmbeddingModel, vec: np.ndarray, t: Teaching | None, members: Sequence[str]
) -> Suggestion:
    """The built-in guess, moved by the owner's teaching as it grows (one blended score)."""
    p_built, junk_like, keep_like = _built_in_score(model, vec)
    jv, weight, support = _teaching_score(t, vec, members) if t is not None else (0.5, 0.0, 0)
    p = weight * jv + (1 - weight) * p_built
    source = (
        "your teaching"
        if weight >= 0.5
        else "built-in"
        if weight == 0
        else "built-in + your teaching"
    )
    if p >= SURE:
        reason = (
            f"Like {max(support, 1)} pictures you marked junk"
            if weight >= 0.5
            else f"Looks like {junk_like}"
        )
        return Suggestion("junk", reason, source)
    if p <= 1 - SURE:
        reason = "Like pictures you marked keep" if weight >= 0.5 else f"Looks like {keep_like}"
        return Suggestion("keep", reason, source)
    return Suggestion(None, "Not sure", source)


def medoid_vectors(db: Database, cluster_ids: Sequence[int], version: int) -> dict[int, np.ndarray]:
    if not cluster_ids:
        return {}
    marks = ",".join("?" for _ in cluster_ids)
    with db.read() as conn:
        rows = conn.execute(
            f"SELECT k.id, c.embedding FROM cluster k JOIN content c ON c.hash = k.medoid_hash "
            f"WHERE k.id IN ({marks}) AND c.embedding IS NOT NULL AND c.embed_version = ?",
            (*cluster_ids, version),
        ).fetchall()
    return {int(i): from_blob(b) for i, b in rows}
