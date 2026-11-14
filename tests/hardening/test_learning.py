"""Hardening (external review, priority 2): analysis and learning never treat an unknown as a
safe answer."""

import pytest

from sift.analysis.ocr import OcrResult, run_ocr
from tests.backup.test_confirm import backed_up
from tests.backup.test_run import Rig


def readable_images(rig: Rig, n: int = 3) -> list[str]:
    backed_up(rig, ["WhatsApp media"])
    with rig.ctx.db.tx() as conn:
        hashes = [
            str(r[0])
            for r in conn.execute(
                "SELECT hash FROM content WHERE kind = 'image' ORDER BY hash LIMIT ?", (n,)
            )
        ]
        conn.executemany(
            "UPDATE content SET analysis_version = 1, width = 1200, height = 1600, "
            "has_camera_exif = 0, unreadable = 0 WHERE hash = ?",
            [(h,) for h in hashes],
        )
    return hashes


def ocr_state(rig: Rig, digest: str) -> tuple:
    with rig.ctx.db.read() as conn:
        return conn.execute(
            "SELECT ocr_version, ocr_text, ocr_error FROM content WHERE hash = ?", (digest,)
        ).fetchone()


def test_a_failed_text_reading_is_not_a_reading_and_is_retried(rig: Rig) -> None:
    hashes = readable_images(rig, 2)

    assert run_ocr(rig.ctx.db, reader=lambda p: None) == 0
    for h in hashes:
        version, text, error = ocr_state(rig, h)
        assert version == 0 and text is None and error  # not checked, failure recorded
    assert run_ocr(rig.ctx.db, reader=lambda p: (_ for _ in ()).throw(RuntimeError("x"))) == 0
    assert all(ocr_state(rig, h)[0] == 0 for h in hashes)  # an exception is a failure too
    assert run_ocr(rig.ctx.db, reader=lambda p: OcrResult("", 0.0, 0.0)) == 2  # retried
    for h in hashes:
        version, text, error = ocr_state(rig, h)
        assert version >= 1 and text == "" and error is None  # read: no text found


def test_an_image_whose_words_could_not_be_read_is_never_listed_as_junk(rig: Rig) -> None:
    from sift.cleanup.junk import _items, never_scored

    hashes = readable_images(rig, 2)
    run_ocr(rig.ctx.db, reader=lambda p: None)
    items = {i.hash: i for i in _items(rig.ctx.db, 1, 2, never_scored(rig.ctx.config))}
    assert all(not items[h].ocr_done for h in hashes if h in items)
    assert any(h in items for h in hashes)


def test_group_rows_share_a_validation_fold() -> None:
    import numpy as np

    from sift.cleanup.classifier import _folds

    y = np.array([1] * 40 + [0] * 40, dtype=float)
    groups = ["big"] * 30 + [f"j{n}" for n in range(10)] + [f"k{n}" for n in range(40)]
    fold = _folds(y, np.random.default_rng(0), groups)
    assert len(set(fold[:30])) == 1  # the 30 pictures labelled together: one fold
    assert len(set(fold)) > 1


def test_one_group_label_counts_as_a_few_decisions_not_hundreds() -> None:
    from sift.cleanup.junk import GROUP_WEIGHT, Item, effective_counts

    def item(h: str, label: str, group: str | None = None) -> Item:
        i = Item(h, "image", 1, 1, 1, None, False, False, False, None, None, None)
        i.label, i.label_group = label, group
        return i

    items = [item(f"g{n}", "junk", "c7") for n in range(500)] + [item("own", "junk")]
    items += [item(f"k{n}", "keep") for n in range(3)]
    counts = effective_counts(items)
    assert counts["junk"] == pytest.approx(GROUP_WEIGHT + 1)  # 500 group members ~ 5 decisions
    assert counts["keep"] == 3


def test_a_bulk_label_cannot_start_the_model_alone(rig: Rig) -> None:
    """500 pictures labelled junk in one group click do not meet the 30-junk minimum."""
    import numpy as np

    from sift.cleanup.junk import Item, train

    def item(h: str, label: str, group: str | None, v: float) -> Item:
        i = Item(h, "image", 1, 1, 1, None, False, False, False, None, None,
                 np.full(8, v, dtype=np.float32))  # fmt: skip
        i.label, i.label_group = label, group
        return i

    items = [item(f"g{n}", "junk", "c1", 0.1 + n * 1e-4) for n in range(500)]
    items += [item(f"k{n}", "keep", None, -0.1 - n * 1e-3) for n in range(40)]
    feats = np.zeros((len(items), 3))
    model = train(items, feats, rig.ctx.config, 1)
    assert model.logreg is None  # cold start: not enough independent junk decisions


def test_a_pictures_own_label_survives_a_later_group_label(rig: Rig) -> None:
    from tests.cleanup.test_browse import client

    hashes = readable_images(rig, 2)
    with rig.ctx.db.tx() as conn:
        conn.execute("INSERT INTO cluster (id, algo_version, size) VALUES (7, 1, 2)")
        conn.executemany(
            "INSERT INTO cluster_member (cluster_id, content_hash) VALUES (7, ?)",
            [(h,) for h in hashes],
        )
    c, h = client(rig)
    c.post("/api/labels", headers=h, json={"hashes": [hashes[0]], "label": "keep"})
    c.post("/api/labels", headers=h, json={"hashes": [], "label": "junk", "cluster_id": 7})
    with rig.ctx.db.read() as conn:
        got = dict(conn.execute("SELECT content_hash, label FROM label").fetchall())
    assert got == {hashes[0]: "keep", hashes[1]: "junk"}
