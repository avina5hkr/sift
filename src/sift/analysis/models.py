"""The embedding model's files: where they come from, their pinned hashes, and the download.

Nothing is downloaded without the owner's consent; the size is shown first (spec 8 G). Each
file is fetched to a `.part` name, its SHA-256 checked against the pinned value, and only then
renamed into place; a file that does not match is discarded (plan assumption D2).
"""

import hashlib
import logging
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("sift.analysis")

CHUNK = 1 << 20


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ModelSpec:
    key: str
    version: int
    files: tuple[ModelFile, ...]

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


_HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/6b7c523703ae26e39be4b03774966f8571c7f523"
CLIP = ModelSpec(
    "clip-vit-b32-onnx",
    1,
    (
        ModelFile(
            "vision_model_quantized.onnx",
            f"{_HF}/onnx/vision_model_quantized.onnx",
            "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299",
            89_117_001,
        ),
        ModelFile(
            "text_model_quantized.onnx",
            f"{_HF}/onnx/text_model_quantized.onnx",
            "73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a",
            64_504_507,
        ),
        ModelFile(
            "tokenizer.json",
            f"{_HF}/tokenizer.json",
            "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd",
            2_224_119,
        ),
    ),
)


class ConsentNeeded(Exception):
    """The model is downloaded only after the owner agrees."""


class ModelDownloadError(Exception):
    """A model file could not be downloaded, or it was not the file expected."""


def folder(models_dir: Path, spec: ModelSpec = CLIP) -> Path:
    return models_dir / spec.key


def installed(models_dir: Path, spec: ModelSpec = CLIP) -> bool:
    """All files present with the expected sizes (their hashes were checked when downloaded)."""
    base = folder(models_dir, spec)
    return all(
        (base / f.name).is_file() and (base / f.name).stat().st_size == f.size for f in spec.files
    )


Opener = Callable[[str], object]


def download(
    models_dir: Path,
    *,
    consent: bool,
    spec: ModelSpec = CLIP,
    opener: Opener = lambda url: urllib.request.urlopen(url, timeout=60),
    should_stop: Callable[[], bool] = lambda: False,
    on_bytes: Callable[[int, int], None] | None = None,
) -> None:
    """Fetch every missing file, checking each one's SHA-256 before it is kept."""
    if not consent:
        raise ConsentNeeded(
            f"Sift needs your permission to download the model ({spec.size // 1_000_000} MB)."
        )
    base = folder(models_dir, spec)
    base.mkdir(parents=True, exist_ok=True)
    done = sum(f.size for f in spec.files if (base / f.name).is_file())
    for f in spec.files:
        final = base / f.name
        if final.is_file() and final.stat().st_size == f.size:
            continue
        part = base / f"{f.name}.part"
        digest = hashlib.sha256()
        try:
            with opener(f.url) as resp, open(part, "wb") as out:  # type: ignore[attr-defined]
                while chunk := resp.read(CHUNK):
                    if should_stop():
                        raise ModelDownloadError("The download was stopped.")
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_bytes is not None:
                        on_bytes(done, spec.size)
        except ModelDownloadError:
            part.unlink(missing_ok=True)
            raise
        except Exception as e:
            part.unlink(missing_ok=True)
            raise ModelDownloadError(
                "Sift could not download the model. Check the internet connection."
            ) from e
        if digest.hexdigest() != f.sha256:
            part.unlink(missing_ok=True)
            raise ModelDownloadError(
                f"The downloaded {f.name} was not the expected file; it was discarded."
            )
        os.replace(part, final)
        log.info("Downloaded %s", f.name)
