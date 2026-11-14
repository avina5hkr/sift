"""Hardening follow-up (second review): text readings belong to the languages they were made in,
group labels count as a few decisions everywhere in learning, and model suggestions are not
ticked unless the owner opts in."""

import numpy as np
import pytest

from sift.analysis.ocr import OcrResult, pending, run_ocr
from sift.cleanup.classifier import GROUP_WEIGHT, LogReg, _balanced, _isotonic, _platt, knn_vote
from tests.backup.test_run import Rig
from tests.hardening.test_learning import readable_images

BOTH, ENGLISH = ["hi-IN", "en-US"], ["en-US"]


def _with_langs(cfg, langs):
    return cfg.model_copy(
        update={"analysis": cfg.analysis.model_copy(update={"ocr_languages": langs})}
    )


# -- OCR languages ---------------------------------------------------------------------------


def test_changing_the_text_languages_reopens_and_rereads_every_reading(rig: Rig) -> None:
    from sift.cleanup.junk import _items, model_fingerprint, never_scored

    hashes = readable_images(rig, 2)
    db = rig.ctx.db
    assert run_ocr(db, langs=BOTH, reader=lambda p: OcrResult("hello", 1.0, 0.1)) == 2
    assert pending(db, None, BOTH) == []  # read for these languages: current

    # The setting changes: every reading is stale, never treated as checked, and read again
    assert {r[0] for r in pending(db, None, ENGLISH)} == set(hashes)
    never = never_scored(rig.ctx.config)
    stale = {i.hash: i for i in _items(db, 1, 2, never, ENGLISH)}
    assert all(not stale[h].ocr_done for h in hashes if h in stale)
    assert any(h in stale for h in hashes)
    with db.read() as conn:
        before = model_fingerprint(conn, rig.ctx.config, 2)
        after = model_fingerprint(conn, _with_langs(rig.ctx.config, ENGLISH), 2)
    assert before != after  # a calibration approval ends

    assert run_ocr(db, langs=ENGLISH, reader=lambda p: OcrResult("hello", 1.0, 0.1)) == 2
    assert pending(db, None, ENGLISH) == []
    fresh = {i.hash: i for i in _items(db, 1, 2, never, ENGLISH)}
    assert all(fresh[h].ocr_done for h in hashes if h in fresh)


def test_clean_slate_keeps_an_image_read_for_other_languages(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sift.cleanup.slate import _infos

    hashes = readable_images(rig, 1)
    with rig.ctx.db.tx() as conn:
        conn.execute("UPDATE content SET analysis_version = 2")
    run_ocr(rig.ctx.db, langs=BOTH, reader=lambda p: OcrResult("", 0.0, 0.0))
    assert _infos(rig.ctx, hashes)[hashes[0]].analysed
    monkeypatch.setattr(rig.ctx.config.analysis, "ocr_languages", ENGLISH)
    assert not _infos(rig.ctx, hashes)[hashes[0]].analysed  # not checked: Clean slate keeps it


def test_readings_from_before_this_change_stay_current_for_the_default_languages(
    rig: Rig,
) -> None:
    hashes = readable_images(rig, 1)
    with rig.ctx.db.tx() as conn:  # what the earlier code stored: the languages Vision read
        conn.execute(
            "UPDATE content SET ocr_version = 1, ocr_text = '', ocr_lang = 'hi-IN+en-US' "
            "WHERE hash = ?",
            (hashes[0],),
        )
    assert pending(rig.ctx.db, None, BOTH) == []


# -- group evidence in calibration -----------------------------------------------------------


def test_platt_and_isotonic_use_the_evidence_weights() -> None:
    rng = np.random.default_rng(3)
    z = rng.normal(size=40)
    y = (z + rng.normal(scale=1.0, size=40) > 0).astype(float)
    w = np.where(np.arange(40) < 10, 2.0, 1.0)  # the first ten count twice
    dz, dy = np.concatenate([z, z[:10]]), np.concatenate([y, y[:10]])
    assert np.allclose(_platt(z, y, w), _platt(dz, dy, np.ones(50)), atol=1e-6)
    xs, ys = _isotonic(z, y, w)
    dxs, dys = _isotonic(dz, dy)
    grid = np.linspace(z.min(), z.max(), 101)
    assert np.allclose(np.interp(grid, xs, ys), np.interp(grid, dxs, dys))
    assert not np.allclose(_platt(z, y, w), _platt(z, y, np.ones(40)))  # weights matter


def test_class_balance_uses_weighted_totals() -> None:
    y = np.array([1.0] * 300 + [0.0] * 30)
    sw = np.array([GROUP_WEIGHT / 300] * 300 + [1.0] * 30)  # one group click against 30 labels
    b = _balanced(y, sw) * sw
    assert b[y == 1].sum() == pytest.approx(b[y == 0].sum())


def test_one_group_click_cannot_switch_calibration_to_isotonic() -> None:
    rng = np.random.default_rng(5)
    x = rng.normal(size=(330, 4))
    y = np.array([1.0] * 300 + [0.0] * 30)
    x[:300, 0] += 2.0
    groups = ["c1"] * 300 + [f"k{n}" for n in range(30)]
    sw = np.array([GROUP_WEIGHT / 300] * 300 + [1.0] * 30)
    lr = LogReg(x, y, groups=groups, weights=sw)
    assert not lr.isotonic  # 35 decisions, not 330 pictures


# -- group evidence in the neighbour votes ---------------------------------------------------


def _ring(centre: np.ndarray, cosines: list[float], rng: np.random.Generator) -> np.ndarray:
    out = []
    for c in cosines:
        r = rng.normal(size=centre.shape)
        r -= r.dot(centre) * centre
        r /= np.linalg.norm(r)
        out.append(c * centre + np.sqrt(1 - c * c) * r)
    return np.array(out, dtype=np.float32)


def test_a_group_takes_at_most_five_neighbour_places() -> None:
    rng = np.random.default_rng(9)
    q = np.zeros(16, dtype=np.float32)
    q[0] = 1.0
    group = _ring(q, [0.96] * 20, rng)  # one group click: 20 junk pictures, very close
    keep = _ring(q, [0.90] * 3, rng)  # three of the owner's own Keep labels, a little further
    labelled = np.vstack([group, keep])
    labels = np.array([1] * 20 + [0] * 3)
    plain = knn_vote(q[None], labelled, labels)
    capped = knn_vote(q[None], labelled, labels, groups=["c7"] * 20 + [None] * 3)
    assert plain.support[0] == 15 and plain.junk_vote[0] == 1.0  # the Keep labels never seen
    assert capped.support[0] == GROUP_WEIGHT  # the group counts as five neighbours
    assert 0.5 < capped.junk_vote[0] < 1.0  # and the Keep labels now have their say
    own = knn_vote(q[None], labelled, labels, groups=[None] * 23)
    assert own.support[0] == 15  # own labels are each a decision, as before


def test_teach_suggestions_weigh_a_group_as_a_few_decisions() -> None:
    from sift.cleanup.classifier import group_weights
    from sift.cleanup.suggest import Teaching, _teaching_score

    rng = np.random.default_rng(11)
    q = np.zeros(16, dtype=np.float32)
    q[0] = 1.0
    vecs = np.vstack([_ring(q, [0.96] * 20, rng), _ring(q, [0.5] * 10, rng)])
    labels = np.array([1] * 20 + [0] * 10)
    groups = ["c3"] * 20 + [None] * 10
    t = Teaching(vecs, labels, [f"h{n}" for n in range(30)], groups, group_weights(groups))
    _jv, say, support = _teaching_score(t, q, [])
    assert support == GROUP_WEIGHT
    closeness = (0.85 - 0.75) / 0.15  # junk neighbours count as at least 0.85 close
    assert say == pytest.approx(15 / 35 * closeness)  # 15 decisions (5 + 10), not 30 pictures


# -- model pre-ticking is opt-in -------------------------------------------------------------


def test_model_suggestions_are_not_ticked_by_default(tmp_path) -> None:
    from sift.core.config import Config, load_config

    assert Config().cleanup.precheck_suggestions is False
    f = tmp_path / "config.toml"
    f.write_text("[cleanup]\nnever_precheck = false\n")  # an existing install's saved setting
    cfg, warnings = load_config(f)
    assert cfg.cleanup.precheck_suggestions is False and not warnings
