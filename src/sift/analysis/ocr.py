"""Stage F, OCR: text in screenshots, greetings and documents, read with Apple Vision.

`VNRecognizeTextRequest` at the accurate level, for en-US and hi-IN when Vision supports them
(checked at run time; plan assumption D1). Stored: normalised text (NFKC, lowercase), the
language asked for, and text density (area of the text boxes / image area). Camera photos,
images under 120 px on the short side, and unreadable images are skipped.
"""

import logging
import os
import unicodedata
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from sift.core.db import Database

log = logging.getLogger("sift.analysis")

VERSION = 1
MIN_SHORT_SIDE = 120
BATCH = 64
WANTED = ("hi-IN", "en-US")  # Hindi first: Vision reads Devanagari only then; Latin still read


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float
    density: float


def lang_key(langs: Sequence[str]) -> str:
    """How a reading records the languages it was asked for: the configured list, in order.
    A reading made for another list is stale (external review follow-up): it counts as not
    checked and is read again."""
    return "+".join(langs)


def reading_current(version: int | None, lang: str | None, langs: Sequence[str]) -> bool:
    """This image's stored reading is from this reader version and these languages."""
    return int(version or 0) >= VERSION and lang == lang_key(langs)


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


@cache
def languages(wanted: Sequence[str] = WANTED) -> tuple[str, ...]:
    """The wanted languages (in order) that Vision on this Mac can read."""
    try:
        import Vision  # type: ignore[import-not-found,import-untyped,unused-ignore]

        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        found, _err = req.supportedRecognitionLanguagesAndReturnError_(None)
        have = set(found or ())
    except Exception:
        return ()
    langs = tuple(lang for lang in wanted if lang in have)
    if "hi-IN" in wanted and "hi-IN" not in langs:
        log.warning("Vision cannot read Hindi on this Mac; Hindi text in images is not read")
    return langs


def recognize(path: Path, langs: Sequence[str] | None = None) -> OcrResult | None:
    """Text in the image at `path`, or None when Vision cannot read the file."""
    try:
        import objc  # type: ignore[import-not-found,import-untyped,unused-ignore]
        import Vision  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from Foundation import NSURL  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError:  # pragma: no cover - installed on macOS
        return None
    langs = languages() if langs is None else tuple(langs)
    if not langs:
        return None
    with objc.autorelease_pool():
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(list(langs))
        req.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
            NSURL.fileURLWithPath_(str(path)), None
        )
        ok, error = handler.performRequests_error_([req], None)
        if not ok:
            log.info("OCR could not read %s: %s", path.name, error)
            return None
        lines, confidences, area = [], [], 0.0
        for obs in req.results() or ():
            best = obs.topCandidates_(1)
            if not best:
                continue
            lines.append(str(best[0].string()))
            confidences.append(float(best[0].confidence()))
            box = obs.boundingBox()  # normalised to the image: 0..1
            area += float(box.size.width) * float(box.size.height)
    text = normalise("\n".join(lines))
    conf = sum(confidences) / len(confidences) if confidences else 0.0
    return OcrResult(text, conf, min(1.0, area))


def pending(
    db: Database, dest_ids: set[int] | None, langs: Sequence[str] = WANTED
) -> list[tuple[str, str, str, int]]:
    """(hash, root, rel_path, dest id) of readable non-camera images without a current reading
    (not read at VERSION, or read for other languages)."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT c.hash, d.root_path, r.rel_path, d.id FROM content c "
            "JOIN replica r ON r.content_hash = c.hash JOIN destination d ON d.id = r.dest_id "
            "WHERE c.kind = 'image' AND c.unreadable = 0 AND c.has_camera_exif = 0 "
            "AND c.analysis_version >= 1 AND (c.ocr_version < ? OR c.ocr_lang IS NOT ?) "
            "AND MIN(IFNULL(c.width, 0), IFNULL(c.height, 0)) >= ? "
            "ORDER BY c.hash, CASE d.kind WHEN 'mac' THEN 0 ELSE 1 END",
            (VERSION, lang_key(langs), MIN_SHORT_SIDE),
        ).fetchall()
    return [
        (str(h), str(root), str(rel), int(d))
        for h, root, rel, d in rows
        if dest_ids is None or int(d) in dest_ids
    ]


def run_ocr(
    db: Database,
    *,
    langs: Sequence[str] = WANTED,
    threads: int = 0,
    dest_ids: set[int] | None = None,
    reader: Callable[[Path], OcrResult | None] = recognize,
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[int, int], None] | None = None,
) -> int:
    """OCR every pending image once; returns how many were read."""
    rows = pending(db, dest_ids, langs)
    copies: dict[str, list[Path]] = {}
    for digest, root, rel, _d in rows:
        copies.setdefault(digest, []).append(Path(root).expanduser() / rel)
    found_langs = languages(tuple(langs)) if reader is recognize else ("test",)
    if not found_langs:  # Vision could not be asked: try again on the next Analyze
        return 0
    lang = lang_key(langs)  # what was asked for: a change of the setting makes it stale
    todo = [(d, next((p for p in ps if p.is_file()), None)) for d, ps in copies.items()]
    todo = [(d, p) for d, p in todo if p is not None]
    done = n = 0
    # Vision requests are independent: several images are read at once, results are written
    # here, one transaction per batch
    with ThreadPoolExecutor(max_workers=threads or workers()) as pool:
        for start in range(0, len(todo), BATCH):
            if should_stop():
                break
            part = todo[start : start + BATCH]
            results = list(pool.map(lambda item: _read(reader, item[1], found_langs), part))
            read = [(d, f) for (d, _p), f in zip(part, results, strict=True) if f is not None]
            failed = [d for (d, _p), f in zip(part, results, strict=True) if f is None]
            with db.tx() as conn:
                # A reading (text, or none found) is stored with its version. A failure is
                # not a reading: the version stays, so the image counts as not checked for
                # document words (never suggested as junk) and the next Analyze tries again.
                conn.executemany(
                    "UPDATE content SET ocr_text = ?, ocr_lang = ?, text_density = ?, "
                    "ocr_version = ?, ocr_error = NULL WHERE hash = ?",
                    [(f.text, lang, f.density, VERSION, d) for d, f in read],
                )
                conn.executemany(
                    "UPDATE content SET ocr_error = 'could not read this image' WHERE hash = ?",
                    [(d,) for d in failed],
                )
            done += sum(found is not None for found in results)
            n += len(part)
            if on_file is not None:
                on_file(n, len(todo))
    return done


def _read(
    reader: Callable[..., OcrResult | None], path: Path, langs: Sequence[str]
) -> OcrResult | None:
    try:
        return reader(path, langs) if reader is recognize else reader(path)
    except Exception as e:  # one bad image is a failed reading, not a failed Analyze
        log.info("OCR failed on %s: %s", path.name, e)
        return None


def workers() -> int:
    """Half the cores, at least 2 and at most 8: the app stays responsive."""
    return max(2, min(8, (os.cpu_count() or 4) // 2))
