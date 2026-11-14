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


@pytest.mark.parametrize("kind", ["image", "video"])
def test_look_alike_videos_are_never_pre_ticked(rig: Rig, kind: str) -> None:
    """Two WhatsApp videos whose sampled frames match (same dHash) but whose sound differs:
    listed in Near-duplicates for the owner to choose, never ticked by default."""
    from sift.cleanup.near_tabs import tab_groups
    from tests.cleanup.test_delete import whatsapp

    ids = whatsapp(rig, 2)
    with rig.ctx.db.tx() as conn:
        a, b = (
            conn.execute("SELECT content_hash FROM phone_file WHERE id = ?", (i,)).fetchone()[0]
            for i in ids
        )
        conn.executemany(
            "UPDATE content SET kind = ?, dhash = 12345 WHERE hash = ?", [(kind, a), (kind, b)]
        )
        conn.execute(
            "INSERT INTO near_group (id, kind, group_type, algo_version) VALUES (1, ?, "
            "'wa_forward', 1)",
            (kind,),
        )
        conn.executemany(
            "INSERT INTO near_member (group_id, content_hash, role) VALUES (1, ?, ?)",
            [(a, "keeper"), (b, "copy")],
        )
    groups = tab_groups(rig.ctx.db, 1, "near")
    assert len(groups) == 1 and len(groups[0].copies) == 1  # still shown
    assert groups[0].copies[0].default_checked == (kind == "image")


def test_settings_that_did_nothing_are_gone_and_limits_are_explicit(tmp_path) -> None:
    from sift.core.config import load_config

    f = tmp_path / "config.toml"
    f.write_text(
        "[analysis]\nthumb_px = 512\nocr_provider = 'tesseract'\nembedding_batch = 8\n"
        "neardup_dhash_max = 8\nocr_languages = ['en-US']\nworkers = 3\n"
    )
    cfg, warnings = load_config(f)
    assert not any(
        "thumb_px" in w or "ocr_provider" in w or "embedding_batch" in w for w in warnings
    )
    assert any("neardup_dhash_max" in w for w in warnings)  # over the supported limit: said so
    assert cfg.analysis.neardup_dhash_max == 6
    assert cfg.analysis.ocr_languages == ["en-US"] and cfg.analysis.workers == 3


def test_ocr_uses_the_configured_languages(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    import sift.analysis.ocr as ocr

    readable_images(rig, 1)
    asked: list[tuple] = []
    monkeypatch.setattr(ocr, "languages", lambda wanted=ocr.WANTED: tuple(wanted))

    def fake_recognize(path, langs=None):
        asked.append(tuple(langs))
        return OcrResult("hello", 0.9, 0.1)

    monkeypatch.setattr(ocr, "recognize", fake_recognize)
    ocr.run_ocr(rig.ctx.db, langs=["en-US"], reader=fake_recognize)
    assert asked == [("en-US",)]


def test_a_group_label_stays_a_group_through_save_and_load(rig: Rig, tmp_path) -> None:
    """Review P3.12: a saved group label loads as one group decision, not many single ones."""
    from sift.cleanup.labels_io import export, load

    hashes = readable_images(rig, 3)
    with rig.ctx.db.tx() as conn:
        conn.executemany(
            "INSERT INTO label (content_hash, label, source, cluster_id, created_at) "
            "VALUES (?, 'junk', 'cluster', 7, 1)",
            [(h,) for h in hashes[:2]],
        )
        conn.execute(
            "INSERT INTO label (content_hash, label, source, created_at) VALUES (?, 'keep', "
            "'manual', 1)",
            (hashes[2],),
        )
    saved = export(rig.ctx.db)
    assert {(x["source"], x["group"]) for x in saved["labels"]} == {
        ("cluster", "g7"), ("manual", None)
    }  # fmt: skip
    with rig.ctx.db.tx() as conn:  # a fresh install of the same pictures
        conn.execute("DELETE FROM label")
    load(rig.ctx.db, saved)
    with rig.ctx.db.read() as conn:
        rows = conn.execute("SELECT content_hash, source, cluster_id FROM label").fetchall()
    by = {h: (src, cid) for h, src, cid in rows}
    assert by[hashes[0]][0] == "cluster" and by[hashes[0]][1] == by[hashes[1]][1] < 0
    assert by[hashes[2]] == ("manual", None)


def test_teaching_file_versions_are_checked(rig: Rig) -> None:
    from sift.cleanup.labels_io import load

    old = {"format": "sift-teaching", "version": 1, "labels": [{"hash": "a" * 64, "label": "keep"}]}
    assert load(rig.ctx.db, old)["read"] == 1  # format 1: every label a picture's own
    with pytest.raises(ValueError, match="newer Sift"):
        load(rig.ctx.db, {"format": "sift-teaching", "version": 99, "labels": []})


def test_a_build_bundles_no_teaching_unless_asked() -> None:
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "packaging" / "build_dmg.sh").read_text()
    assert 'TEACHING="${SIFT_TEACHING:-}"' in script and "local/teaching.json" not in script
