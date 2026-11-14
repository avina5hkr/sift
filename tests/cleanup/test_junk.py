"""Junk tiers, P5, recordings, calibration and the Junk tab API (M7, spec 9)."""

import random
from typing import Any

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from sift.analysis.embed import l2, to_blob
from sift.analysis.models import CLIP
from sift.cleanup import calibration
from sift.cleanup.candidates import rebuild_exact, set_selection
from sift.cleanup.junk import (
    Item,
    Model,
    _features,
    ensure_fresh,
    rebuild_junk,
    state,
    train,
    verdicts,
)
from sift.cleanup.lexicons import load
from sift.core.config import Config

DIM = 16
WA = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/"
_rng = np.random.default_rng(7)
A = l2(_rng.normal(size=(1, DIM)))[0]  # the junk look
B = l2(_rng.normal(size=(1, DIM)))[0]  # the keep look


def near(centre: np.ndarray, rng: np.random.Generator, noise: float = 0.03) -> np.ndarray:
    return l2(centre + noise * rng.normal(size=(1, DIM)))[0].astype(np.float32)


def seed(db: Any, rows: list[dict[str, Any]]) -> None:
    with db.tx() as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO content (hash, size, kind, width, height, duration_ms, "
                "has_camera_exif, is_screenshot, ocr_text, embedding, embed_version, ocr_version, "
                "analysis_version, created_at) "
                "VALUES (?, ?, ?, 800, 800, ?, ?, ?, ?, ?, ?, ?, 2, 1)",
                (
                    r["hash"], r.get("size", 60_000), r.get("kind", "image"), r.get("duration"),
                    int(r.get("exif", 0)), int(r.get("shot", 0)), r.get("text"),
                    to_blob(r["emb"]) if r["emb"] is not None else None, CLIP.version,
                    r.get("ocr", 1),
                ),
            )  # fmt: skip
            for n, path in enumerate(r.get("paths") or [f"{WA}{r['hash']}.jpg"]):
                conn.execute(
                    "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, "
                    "content_hash, state, present) VALUES (1, ?, ?, ?, ?, ?, 'done', 1)",
                    (path, r.get("origin", "whatsapp_received"), r.get("size", 60_000),
                     1_600_000_000 + n, r["hash"]),
                )  # fmt: skip
            if r.get("label"):
                conn.execute(
                    "INSERT INTO label (content_hash, label, source, created_at) "
                    "VALUES (?, ?, 'manual', 1)",
                    (r["hash"], r["label"]),
                )


def taught(n_each: int = 35, seed_no: int = 1) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed_no)
    rows = [{"hash": f"j{i}", "emb": near(A, rng), "label": "junk"} for i in range(n_each)]
    rows += [{"hash": f"k{i}", "emb": near(B, rng), "label": "keep"} for i in range(n_each)]
    return rows


def unlabelled() -> list[dict[str, Any]]:
    rng = np.random.default_rng(99)
    return [
        {"hash": "u_greet", "emb": near(A, rng), "text": "good morning friends"},
        {"hash": "u_plain", "emb": near(A, rng)},
        {"hash": "u_exif", "emb": near(A, rng), "exif": 1, "text": "good morning"},
        {"hash": "u_doc", "emb": near(A, rng), "text": "good morning\ninvoice no 42"},
        {"hash": "u_pan", "emb": near(A, rng), "text": "good morning abcde1234f"},
        {"hash": "u_shot", "emb": near(A, rng), "shot": 1, "text": "good morning"},
        {"hash": "u_cam", "emb": near(A, rng), "origin": "camera", "text": "good morning",
         "paths": ["/sdcard/DCIM/Camera/u_cam.jpg"]},
        {"hash": "u_keep", "emb": near(B, rng), "text": "good morning"},
        {"hash": "u_rec", "emb": near(A, rng), "text": "good morning",
         "paths": ["/sdcard/Recordings/Call/u_rec.jpg"]},
    ]  # fmt: skip


def tiers(db: Any) -> dict[str, str]:
    with db.read() as conn:
        return {
            str(h): str(t)
            for h, t in conn.execute(
                "SELECT p.content_hash, c.tab FROM candidate c JOIN phone_file p "
                "ON p.id = c.phone_file_id WHERE c.tab IN ('junk', 'maybe')"
            )
        }


def test_tiers_protect_first_and_need_a_corroboration(rig) -> None:
    seed(rig.ctx.db, taught() + unlabelled())
    counts = rebuild_junk(rig.ctx.db, 1, rig.ctx.config, CLIP.version)
    got = tiers(rig.ctx.db)
    assert got["u_greet"] == "junk"  # high score, support, and a greeting hit
    assert got["u_plain"] == "maybe"  # high score but nothing corroborates it
    for protected in ("u_exif", "u_doc", "u_pan", "u_shot", "u_cam", "u_keep"):
        assert protected not in got, protected
    assert all(f"j{i}" in got and got[f"j{i}"] == "junk" for i in range(35))  # owner's own call
    assert not any(h.startswith("k") for h in got)
    assert counts["suggested"] >= 36 and counts["protected"] >= 5


def test_files_whose_protection_is_unknown_are_not_listed(rig) -> None:
    rng = np.random.default_rng(5)
    seed(rig.ctx.db, [
        *taught(),
        {"hash": "n_ocr", "emb": near(A, rng), "ocr": 0},  # its words are not read yet
        {"hash": "n_vid", "emb": near(A, rng), "kind": "video", "duration": None,
         "paths": [f"{WA}n_vid-{n}.mp4" for n in range(3)]},
        {"hash": "n_emb", "emb": None, "text": "good morning",
         "paths": [f"{WA}n_emb-{n}.jpg" for n in range(3)]},
        {"hash": "n_shot", "emb": near(A, rng), "text": "good morning", "origin": "screenshot"},
    ])  # fmt: skip
    rebuild_junk(rig.ctx.db, 1, rig.ctx.config, CLIP.version)
    got = tiers(rig.ctx.db)
    assert not {"n_ocr", "n_vid", "n_emb", "n_shot"} & set(got)


def test_the_tabs_follow_settings_and_drop_stale_ticks(rig) -> None:
    db, cfg = rig.ctx.db, rig.ctx.config
    seed(db, taught() + unlabelled())
    rebuild_junk(db, 1, cfg, CLIP.version)
    assert not ensure_fresh(db, 1, cfg, CLIP.version)  # nothing changed
    with db.read() as conn:
        pid = conn.execute("SELECT id FROM phone_file WHERE content_hash = 'u_greet'").fetchone()[0]
    set_selection(db, [pid], True)
    cfg.scoring.protected_folders = [WA]  # the owner protects the WhatsApp folder
    assert ensure_fresh(db, 1, cfg, CLIP.version)
    assert "u_greet" not in tiers(db)
    with db.read() as conn:  # its tick went with it
        assert conn.execute("SELECT count(*) FROM selection").fetchone()[0] == 0


def test_training_uses_labels_of_files_no_longer_on_the_phone(rig) -> None:
    db, cfg = rig.ctx.db, rig.ctx.config
    seed(db, taught() + unlabelled())
    with db.tx() as conn:  # the labelled files were deleted from the phone
        conn.execute("UPDATE phone_file SET present = 0 WHERE content_hash LIKE 'j%' "
                     "OR content_hash LIKE 'k%'")  # fmt: skip
    rebuild_junk(db, 1, cfg, CLIP.version)
    assert tiers(db).get("u_greet") == "junk"


def test_recordings_never_junk(rig) -> None:
    seed(rig.ctx.db, taught() + unlabelled())
    rebuild_junk(rig.ctx.db, 1, rig.ctx.config, CLIP.version)
    assert "u_rec" not in tiers(rig.ctx.db)


def test_cold_start_lists_lexicon_hits_with_copies_under_maybe_only(rig) -> None:
    rng = np.random.default_rng(3)
    rows = [
        *taught(n_each=5),
        {"hash": "c3", "emb": near(A, rng), "text": "good night",
         "paths": [f"{WA}c3-{n}.jpg" for n in range(3)]},
        {"hash": "c1", "emb": near(A, rng), "text": "good night"},
    ]  # fmt: skip
    seed(rig.ctx.db, rows)
    rebuild_exact(rig.ctx.db, 1)  # c3's paths are exact duplicates too; junk still lists them
    rebuild_junk(rig.ctx.db, 1, rig.ctx.config, CLIP.version)
    got = tiers(rig.ctx.db)
    assert got["c3"] == "maybe" and "c1" not in got
    assert {t for h, t in got.items() if not h.startswith("j")} == {"maybe"}


def test_calibration_passes_then_a_wrong_item_raises_the_threshold(rig, monkeypatch) -> None:
    monkeypatch.setattr(calibration, "MIN_TO_PASS", 5)  # the flow, on a six-item sample
    db, cfg = rig.ctx.db, rig.ctx.config
    extra = [
        {"hash": f"g{i}", "emb": near(A, np.random.default_rng(200 + i)), "text": "good morning"}
        for i in range(5)
    ]
    seed(db, taught() + unlabelled() + extra)
    size = calibration.start(db, 1, cfg, CLIP.version, rng=random.Random(1))
    assert size == 6  # u_greet and g0-g4: model suggestions, not the owner's junk labels
    result = None
    for h, _pid, _s, _v in calibration.items(db):
        result = calibration.mark(db, cfg, h, True, CLIP.version)
    assert result is not None and result.passed and result.errors == 0
    assert state(db, cfg, CLIP.version)["precheck_allowed"]
    rebuild_junk(db, 1, cfg, CLIP.version)
    with db.read() as conn:
        assert (
            conn.execute(
                "SELECT min(default_checked) FROM candidate WHERE tab = 'junk'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT passed FROM calibration").fetchall() == [(1,)]

    calibration.start(db, 1, cfg, CLIP.version, rng=random.Random(2))
    sample = calibration.items(db)
    for n, (h, _pid, _s, _v) in enumerate(sample):
        result = calibration.mark(db, cfg, h, n != 0, CLIP.version)  # the first one is wrong
    assert result is not None and not result.passed and result.errors == 1
    st_ = state(db, cfg, CLIP.version)
    assert not st_["precheck_allowed"]
    assert st_["t_suggest"] == sample[0][2] + 0.01  # not capped at 1
    with db.read() as conn:
        assert conn.execute(
            "SELECT label, source FROM label WHERE content_hash = ?", (sample[0][0],)
        ).fetchone() == ("keep", "calibration")


def test_the_junk_tab_and_status_over_the_api(rig) -> None:
    from tests.helpers import H, client_for

    seed(rig.ctx.db, taught() + unlabelled())
    client = client_for(rig.ctx)
    status = client.get("/api/cleanup/junk/status", headers=H).json()
    assert not status["cold_start"] and status["suggested"] >= 36 and status["maybe"] >= 1
    body = client.get("/api/cleanup/candidates?tab=junk&sort=score", headers=H).json()
    greet = next(g for g in body["groups"] if g["content_hash"] == "u_greet")
    assert greet["keeper"] is None and greet["copies"][0]["checked"] is False  # no calibration
    assert {"code": "text", "text": "Text: good morning"} in greet["reasons"]
    # Not junk: the file leaves the tab after the retrain
    client.post(
        "/api/labels",
        headers=H,
        json={"hashes": ["u_greet"], "label": "keep", "source": "feedback"},
    )
    body = client.get("/api/cleanup/candidates?tab=junk", headers=H).json()
    assert "u_greet" not in {g["content_hash"] for g in body["groups"]}
    # u_greet was the only model suggestion; the rest are the owner's own junk labels
    refused = client.post("/api/cleanup/calibration/start", headers=H)
    assert refused.status_code == 409
    assert client.get("/api/cleanup/calibration", headers=H).json()["total"] == 0


PROTECT_FLAGS = st.fixed_dictionaries(
    {
        "exif": st.booleans(),
        "shot": st.booleans(),
        "shot_origin": st.booleans(),
        "folder": st.booleans(),
        "camera": st.booleans(),
        "doc": st.booleans(),
        "unreadable": st.booleans(),
        "label": st.sampled_from([None, "protected", "keep", "junk"]),
        "long_video": st.booleans(),
        "copies": st.integers(0, 5),
        "greet": st.booleans(),
    }
)


@settings(max_examples=150, deadline=None)
@given(st.lists(PROTECT_FLAGS, min_size=1, max_size=12), st.integers(0, 10_000))
def test_p5_no_protected_file_is_ever_a_candidate(flags: list[dict[str, Any]], salt: int) -> None:
    rng = np.random.default_rng(salt)
    cfg = Config()
    cfg.scoring.protected_folders = ["/sdcard/Keep"]
    greet, doc = load("greeting"), load("document")
    labelled = [
        Item(f"j{i}", "image", 50_000, 800, 800, None, False, False, False, "good morning", 0.1,
             near(A, rng), [(i, f"{WA}j{i}.jpg", "whatsapp_received", 1)], 2, "junk")
        for i in range(32)
    ] + [
        Item(f"k{i}", "image", 50_000, 800, 800, None, False, False, False, None, 0.0,
             near(B, rng), [(100 + i, f"{WA}k{i}.jpg", "whatsapp_received", 1)], 0, "keep")
        for i in range(32)
    ]  # fmt: skip
    probes = []
    for n, f in enumerate(flags):
        text = (
            " ".join(t for t, on in (("good morning", f["greet"]), ("ticket", f["doc"])) if on)
            or None
        )
        probes.append(
            Item(f"p{n}", "video" if f["long_video"] else "image", 40_000, 800, 800,
                 11 * 60_000 if f["long_video"] else None, f["exif"], f["shot"], f["unreadable"],
                 text, 0.1, near(A, rng),
                 [(500 + n, f"/sdcard/Keep/p{n}.jpg" if f["folder"] else f"{WA}p{n}.jpg",
                   "camera" if f["camera"] else "whatsapp_received", 1),
                  *([(900 + n, f"/sdcard/Pictures/Screenshots/p{n}.png", "screenshot", 1)]
                    if f["shot_origin"] else [])],
                 f["copies"], f["label"])
        )  # fmt: skip
    items = labelled + probes
    feats = _features(items, greet, doc, 1_700_000_000)
    model: Model = train(items, feats, cfg, 1)
    found = verdicts(items, feats, model, cfg, t_suggest=0.5, greet=greet, doc=doc)
    for f, v in zip(flags, found[len(labelled) :], strict=True):
        is_protected = (
            f["exif"] or f["shot"] or f["camera"] or f["doc"] or f["unreadable"]
            or f["shot_origin"] or f["folder"]
            or f["label"] == "protected" or f["long_video"]
        )  # fmt: skip
        if is_protected:
            assert v.tier == "protected", (f, v)
        if is_protected or f["label"] == "keep":
            assert v.tier not in ("suggested", "maybe"), (f, v)


def _six_item_sample(rig):
    extra = [
        {"hash": f"g{i}", "emb": near(A, np.random.default_rng(200 + i)), "text": "good morning"}
        for i in range(5)
    ]
    seed(rig.ctx.db, taught() + unlabelled() + extra)


def test_a_small_calibration_teaches_but_never_turns_on_pre_ticking(rig) -> None:
    """Review P2.6: one or a few suggested items cannot prove the model is right."""
    db, cfg = rig.ctx.db, rig.ctx.config
    _six_item_sample(rig)
    assert calibration.start(db, 1, cfg, CLIP.version, rng=random.Random(1)) == 6
    result = None
    for h, _pid, _s, _v in calibration.items(db):
        result = calibration.mark(db, cfg, h, True, CLIP.version)
    assert result is not None and result.too_few and not result.passed
    assert not state(db, cfg, CLIP.version)["precheck_allowed"]
    calibration.start(db, 1, cfg, CLIP.version, rng=random.Random(2))
    sample = calibration.items(db)
    for n, (h, _pid, _s, _v) in enumerate(sample):
        calibration.mark(db, cfg, h, n != 0, CLIP.version)
    with db.read() as conn:  # the owner's "wrong" still teaches the model
        assert conn.execute(
            "SELECT label FROM label WHERE content_hash = ?", (sample[0][0],)
        ).fetchone() == ("keep",)


def test_an_approval_ends_when_a_word_list_changes(rig, monkeypatch, tmp_path) -> None:
    from sift.cleanup.lexicons import source_text

    monkeypatch.setattr(calibration, "MIN_TO_PASS", 5)
    db, cfg = rig.ctx.db, rig.ctx.config
    words = tmp_path / "lexicons"
    words.mkdir()
    (words / "greeting.yaml").write_text(source_text("greeting", None))
    _six_item_sample(rig)
    calibration.start(db, 1, cfg, CLIP.version, words, rng=random.Random(1))
    for h, _pid, _s, _v in calibration.items(db):
        calibration.mark(db, cfg, h, True, CLIP.version, words)
    assert state(db, cfg, CLIP.version, words)["precheck_allowed"]
    (words / "greeting.yaml").write_text(source_text("greeting", None) + "\n# edited\n")
    assert not state(db, cfg, CLIP.version, words)["precheck_allowed"]


def test_a_failed_calibration_never_lowers_a_stricter_threshold(rig, monkeypatch) -> None:
    from sift.cleanup.junk import T_SUGGEST, put_setting

    db, cfg = rig.ctx.db, rig.ctx.config
    _six_item_sample(rig)
    calibration.start(db, 1, cfg, CLIP.version, rng=random.Random(1))
    with db.tx() as conn:  # the threshold was raised meanwhile (an earlier calibration)
        put_setting(conn, T_SUGGEST, "1.5")
    for n, (h, _pid, _s, _v) in enumerate(calibration.items(db)):
        result = calibration.mark(db, cfg, h, n != 0, CLIP.version)
    assert result is not None and result.threshold == 1.5
    assert state(db, cfg, CLIP.version)["t_suggest"] == 1.5
