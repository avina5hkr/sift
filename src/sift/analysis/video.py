"""Stage E: video metadata and fingerprints through AVFoundation (plan assumption C1).

`probe` reads the duration and the display size, and grabs frames at 10, 30, 50, 70 and 90 %
of the duration; each frame's dHash is one entry of the fingerprint, and the 50 % frame is the
video's thumbnail. The file is only read. Anything AVFoundation cannot open is `None`.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from sift.analysis.phash import dhash, distance, to_signed

log = logging.getLogger("sift.analysis")

POSITIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
FRAME_PX = 512


@dataclass(frozen=True)
class VideoInfo:
    duration_ms: int
    width: int
    height: int
    frames: tuple[int, ...]  # signed dHashes, one per position
    poster: Image.Image | None  # the 50 % frame


def _to_pil(cg: object) -> Image.Image:
    import Quartz  # type: ignore[import-not-found,import-untyped,unused-ignore]

    width = Quartz.CGImageGetWidth(cg)
    height = Quartz.CGImageGetHeight(cg)
    # Draw into a known RGBA bitmap, whatever the frame's own pixel format is.
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, space, Quartz.kCGImageAlphaPremultipliedLast
    )
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, width, height), cg)
    data = Quartz.CGBitmapContextGetData(ctx)
    raw = bytes(data.as_buffer(width * height * 4))
    return Image.frombuffer("RGBA", (width, height), raw, "raw", "RGBA", 0, 1).convert("RGB")


def probe(path: Path) -> VideoInfo | None:
    try:
        import AVFoundation  # type: ignore[import-not-found,import-untyped,unused-ignore]
        import CoreMedia  # type: ignore[import-not-found,import-untyped,unused-ignore]
        import Quartz  # type: ignore[import-not-found,import-untyped,unused-ignore]  # noqa: F401  (first: bridges CGImageRef)
        from Foundation import NSURL  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError:  # pragma: no cover - installed on macOS
        return None
    try:
        asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(
            NSURL.fileURLWithPath_(str(path)), None
        )
        seconds = CoreMedia.CMTimeGetSeconds(asset.duration())
        if not seconds or seconds != seconds or seconds <= 0:  # 0, NaN or negative
            return None
        tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeVideo)
        if not tracks:
            return None
        size = tracks[0].naturalSize()
        transform = tracks[0].preferredTransform()
        width, height = abs(int(size.width)), abs(int(size.height))
        if abs(transform.b) == 1 and abs(transform.c) == 1:  # rotated by 90 or 270 degrees
            width, height = height, width
        gen = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
        gen.setAppliesPreferredTrackTransform_(True)
        gen.setMaximumSize_((FRAME_PX, FRAME_PX))
        tolerance = CoreMedia.CMTimeMakeWithSeconds(0.05, 600)
        gen.setRequestedTimeToleranceBefore_(tolerance)
        gen.setRequestedTimeToleranceAfter_(tolerance)
        frames: list[int] = []
        poster: Image.Image | None = None
        for at in POSITIONS:
            when = CoreMedia.CMTimeMakeWithSeconds(seconds * at, 600)
            result = gen.copyCGImageAtTime_actualTime_error_(when, None, None)
            cg, error = result[0], result[-1]
            if cg is None:
                log.info("Could not read a frame of %s: %s", path.name, error)
                return None
            img = _to_pil(cg)
            frames.append(to_signed(dhash(img)))
            if at == 0.5:
                poster = img
        return VideoInfo(int(seconds * 1000), width, height, tuple(frames), poster)
    except Exception as e:
        log.info("Could not read video %s: %s", path.name, e)
        return None


def similar(
    a: VideoInfo | tuple[int, int, int, tuple[int, ...]],
    b: VideoInfo | tuple[int, int, int, tuple[int, ...]],
    *,
    frame_max: int = 10,
    frames_min: int = 4,
    aspect_tol: float = 0.06,
) -> bool:
    """Spec 8 E: durations within max(1 s, 2 %), ≥ 4 of 5 frames within distance 10 by
    position, aspect ratio within 6 %. Takes VideoInfo or (duration_ms, w, h, frames)."""
    da, wa, ha, fa = (a.duration_ms, a.width, a.height, a.frames) if isinstance(a, VideoInfo) else a
    db, wb, hb, fb = (b.duration_ms, b.width, b.height, b.frames) if isinstance(b, VideoInfo) else b
    if abs(da - db) > max(1000, 0.02 * max(da, db)):
        return False
    if not (wa and ha and wb and hb) or abs(wa / ha - wb / hb) > aspect_tol * (wa / ha):
        return False
    close = sum(1 for x, y in zip(fa, fb, strict=False) if distance(x, y) <= frame_max)
    return close >= frames_min
