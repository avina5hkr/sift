"""Teach-the-app suggestions (built in, then moved by teaching) and saving/loading teaching."""

import numpy as np

from sift.analysis.models import CLIP
from sift.cleanup.labels_io import apply_pending, export, load
from sift.cleanup.suggest import JUNK_PROMPTS, Teaching, suggest
from tests.cleanup.test_junk import A, B, near, seed

DIM = 16


class PromptModel:
    """Junk descriptions point at A, keep descriptions at B."""

    dim = DIM
    version = 99

    def encode_text(self, texts: list[str]) -> np.ndarray:
        return np.stack([A if t in JUNK_PROMPTS else B for t in texts]).astype(np.float32)

    def encode_image(self, images: list[object]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


def test_the_built_in_guess_works_with_no_teaching() -> None:
    rng = np.random.default_rng(1)
    model = PromptModel()
    junk = suggest(model, near(A, rng), None, ())
    keep = suggest(model, near(B, rng), None, ())
    assert (junk.label, junk.source) == ("junk", "built-in") and junk.reason.startswith(
        "Looks like"
    )
    assert (keep.label, keep.source) == ("keep", "built-in")


def test_teaching_moves_the_built_in_guess_more_as_it_grows() -> None:
    rng = np.random.default_rng(2)
    model = PromptModel()
    probe = near(A, rng, 0.02)  # built in, this looks like junk...

    def taught(n: int) -> Teaching:  # ...but the owner keeps pictures like it
        vecs = [near(A, rng, 0.02) for _ in range(n)] + [near(B, rng) for _ in range(n)]
        labels = [0] * n + [1] * n
        return Teaching(np.stack(vecs), np.array(labels), [f"h{i}" for i in range(2 * n)])

    few = suggest(model, probe, taught(3), ())
    many = suggest(model, probe, taught(60), ())
    assert few.label != "keep" or few.source != "your teaching"
    assert many.label == "keep" and many.source == "your teaching"


def test_teaching_is_saved_and_loaded_and_waits_for_unknown_pictures(rig) -> None:
    rng = np.random.default_rng(3)
    seed(rig.ctx.db, [{"hash": "a" * 64, "emb": near(A, rng), "label": "junk"}])
    saved = export(rig.ctx.db)
    assert saved["labels"] == [
        {"hash": "a" * 64, "label": "junk", "tag": None, "source": "manual", "group": None}
    ]
    unknown = "b" * 64
    got = load(
        rig.ctx.db,
        {
            "format": "sift-teaching",
            "labels": [*saved["labels"], {"hash": unknown, "label": "keep"}],
        },
    )
    # the first is already labelled here (kept as it is); the second waits for a backup
    assert got == {"read": 2, "applied": 0, "waiting": 1}
    seed(rig.ctx.db, [{"hash": unknown, "emb": near(B, rng)}])  # backed up later
    assert apply_pending(rig.ctx.db) == 1
    with rig.ctx.db.read() as conn:
        assert conn.execute(
            "SELECT label FROM label WHERE content_hash = ?", (unknown,)
        ).fetchone() == ("keep",)
        assert conn.execute("SELECT count(*) FROM label_import").fetchone()[0] == 0
    assert CLIP.version  # the real model is untouched by these tests
