"""Stage G, embeddings: a 512-d, L2-normalised vector per image (videos: their 50 % frame).

`EmbeddingModel` is the interface (encode_image, encode_text, dim, version); `ClipOnnx` is the
default, CLIP ViT-B/32 from the downloaded ONNX towers (int8). Vectors are stored as float16 in
`content.embedding` with `content.embed_version`, so a new model version recomputes them.
"""

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageOps

from sift.analysis.index import thumb_path
from sift.analysis.models import CLIP, ModelSpec, folder
from sift.core.db import Database

log = logging.getLogger("sift.analysis")

BATCH = 32
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
_CONTEXT = 77


class EmbeddingModel(Protocol):
    dim: int
    version: int

    def encode_image(self, images: Sequence[Image.Image]) -> np.ndarray: ...
    def encode_text(self, texts: Sequence[str]) -> np.ndarray: ...


def l2(rows: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    out: np.ndarray = rows / np.maximum(norms, 1e-12)
    return out


def clip_pixels(img: Image.Image, size: int = 224) -> np.ndarray:
    """CLIP preprocessing: shortest side to 224 (bicubic), centre crop, scale, mean/std, CHW."""
    img = img.convert("RGB")
    w, h = img.size
    scale = size / min(w, h)
    img = img.resize(
        (max(size, round(w * scale)), max(size, round(h * scale))), Image.Resampling.BICUBIC
    )
    w, h = img.size
    left, top = (w - size) // 2, (h - size) // 2
    img = img.crop((left, top, left + size, top + size))
    arr = (np.asarray(img, dtype=np.float32) / 255.0 - _MEAN) / _STD
    return arr.transpose(2, 0, 1)


class ClipOnnx:
    dim = 512

    def __init__(self, models_dir: Path, spec: ModelSpec = CLIP) -> None:
        import onnxruntime as ort  # type: ignore[import-untyped]
        from tokenizers import Tokenizer

        ort.set_default_logger_severity(3)
        if hasattr(ort, "disable_telemetry_events"):
            ort.disable_telemetry_events()
        base = folder(models_dir, spec)
        wanted = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        providers = [p for p in wanted if p in ort.get_available_providers()]

        def session(name: str) -> object:
            try:
                return ort.InferenceSession(str(base / name), providers=providers)
            except Exception as e:  # e.g. CoreML cannot compile the graph: use the CPU
                if providers == ["CPUExecutionProvider"]:
                    raise
                log.info("CoreML could not load %s (%s); using the CPU", name, e)
                return ort.InferenceSession(str(base / name), providers=["CPUExecutionProvider"])

        self._vision: Any = session("vision_model_quantized.onnx")
        self._text: Any = session("text_model_quantized.onnx")
        self._tok = Tokenizer.from_file(str(base / "tokenizer.json"))
        self.version = spec.version

    def encode_image(self, images: Sequence[Image.Image]) -> np.ndarray:
        batch = np.stack([clip_pixels(i) for i in images])
        (out,) = self._vision.run(["image_embeds"], {"pixel_values": batch})
        return l2(np.asarray(out, dtype=np.float32))

    def encode_text(self, texts: Sequence[str]) -> np.ndarray:
        rows = []
        for text in texts:  # one at a time: no padding, so the end token is the pooled one
            ids = self._tok.encode(text).ids
            if len(ids) > _CONTEXT:  # the tower pools at the end token: keep it last
                ids = [*ids[: _CONTEXT - 1], ids[-1]]
            (out,) = self._text.run(["text_embeds"], {"input_ids": np.array([ids], dtype=np.int64)})
            rows.append(np.asarray(out, dtype=np.float32)[0])
        return l2(np.stack(rows))


def to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float16).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def _load(path: Path, size: int = 448) -> Image.Image:
    with Image.open(path) as raw:
        if raw.format == "JPEG":
            raw.draft("RGB", (size, size))
        img = ImageOps.exif_transpose(raw)
        w, h = img.size
        scale = size / min(w, h)
        if scale < 1:  # the short side to `size`; CLIP then crops the centre square
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        return img.convert("RGB")


def pending(
    db: Database, version: int, dest_ids: set[int] | None
) -> list[tuple[str, str, str, str]]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT c.hash, c.kind, d.root_path, r.rel_path, d.id FROM content c "
            "JOIN replica r ON r.content_hash = c.hash JOIN destination d ON d.id = r.dest_id "
            "WHERE c.kind IN ('image', 'video') AND c.unreadable = 0 AND c.analysis_version >= 2 "
            "AND c.embed_version < ? ORDER BY c.hash, CASE d.kind WHEN 'mac' THEN 0 ELSE 1 END",
            (version,),
        ).fetchall()
    return [
        (str(h), str(k), str(root), str(rel))
        for h, k, root, rel, d in rows
        if dest_ids is None or int(d) in dest_ids
    ]


def run_embed(
    db: Database,
    model: EmbeddingModel,
    cache: Path,
    *,
    dest_ids: set[int] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[int, int], None] | None = None,
) -> int:
    """Embed every image and video not embedded with this model version; returns how many."""
    rows = pending(db, model.version, dest_ids)
    todo: dict[str, tuple[str, list[Path]]] = {}
    for digest, kind, root, rel in rows:
        todo.setdefault(digest, (kind, []))[1].append(Path(root).expanduser() / rel)
    items = list(todo.items())
    done = 0
    for start in range(0, len(items), BATCH):
        if should_stop():
            break
        hashes, images, failed = [], [], []
        for digest, (kind, paths) in items[start : start + BATCH]:
            source = (
                thumb_path(cache, digest)
                if kind == "video"
                else next((p for p in paths if p.is_file()), None)
            )
            try:
                if source is None or not source.is_file():
                    continue
                images.append(_load(source))
                hashes.append(digest)
            except Exception as e:
                log.info("Could not embed %s: %s", digest[:12], e)
                failed.append(digest)
        found: list[tuple[str, np.ndarray]] = []
        if images:
            try:
                found = list(zip(hashes, model.encode_image(images), strict=True))
            except Exception:
                for h, img in zip(hashes, images, strict=True):
                    try:
                        found.append((h, model.encode_image([img])[0]))
                    except Exception as e:
                        log.info("Could not embed %s: %s", h[:12], e)
                        failed.append(h)
        with db.tx() as conn:
            conn.executemany(
                "UPDATE content SET embedding = ?, embed_version = ? WHERE hash = ?",
                [(to_blob(v), model.version, h) for h, v in found]
                + [(None, model.version, h) for h in failed],
            )
        done += len(found)
        if on_file is not None:
            on_file(min(start + BATCH, len(items)), len(items))
    return done


def search(
    db: Database, model: EmbeddingModel, query: str, *, limit: int = 60
) -> list[tuple[str, float]]:
    """(content hash, cosine) of the images most like the text `query`, best first."""
    q = model.encode_text([query])[0]
    with db.read() as conn:
        rows = conn.execute(
            "SELECT hash, embedding FROM content WHERE embedding IS NOT NULL AND embed_version = ?",
            (model.version,),
        ).fetchall()
    if not rows:
        return []
    matrix = np.stack([from_blob(r[1]) for r in rows])
    scores = matrix @ q
    order = np.argsort(-scores)[:limit]
    return [(str(rows[i][0]), float(scores[i])) for i in order]
