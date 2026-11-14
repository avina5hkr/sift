"""Hardening (external review, priority 2): analysis and learning never treat an unknown as a
safe answer."""

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
