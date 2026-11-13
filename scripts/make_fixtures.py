"""Make the near-duplicate fixtures (spec 16): images and their variants, videos, expected.json.

    uv run python scripts/make_fixtures.py OUT_DIR

Images are drawn with Pillow. Videos are Motion-JPEG QuickTime files written by `write_mov`
below (plan assumption C1: no ffmpeg needed). `expected.json` lists groups that must match,
pairs that must never match, and variants that may be missed (crop, rotation).
"""

import io
import json
import math
import random
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

SIZE = (640, 480)


# -- images ----------------------------------------------------------------------------------


def base_image(seed: int) -> Image.Image:
    """A distinct picture per seed: a gradient, shapes and a stripe of text-like blocks."""
    rnd = random.Random(seed)
    w, h = SIZE
    img = Image.new("RGB", SIZE)
    c1 = [rnd.randrange(256) for _ in range(3)]
    c2 = [rnd.randrange(256) for _ in range(3)]
    draw = ImageDraw.Draw(img)
    angle = rnd.random() * math.tau
    for y in range(h):
        t = (y / h) * abs(math.cos(angle)) + 0.3 * abs(math.sin(angle))
        t = min(1.0, t)
        draw.line(
            [(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2, strict=True))
        )
    for _ in range(8):
        x0, y0 = rnd.randrange(w), rnd.randrange(h)
        x1, y1 = x0 + rnd.randrange(40, 260), y0 + rnd.randrange(40, 200)
        fill = tuple(rnd.randrange(256) for _ in range(3))
        (draw.ellipse if rnd.random() < 0.5 else draw.rectangle)([x0, y0, x1, y1], fill=fill)
    for i in range(6):
        draw.rectangle(
            [40, 40 + i * 22, 40 + rnd.randrange(120, 520), 52 + i * 22], fill=(20, 20, 20)
        )
    return img


def jpeg_bytes(img: Image.Image, quality: int = 90) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def variants(img: Image.Image) -> dict[str, tuple[bytes, str, bool]]:
    """name -> (bytes, extension, must_match)."""
    w, h = img.size
    once = Image.open(io.BytesIO(jpeg_bytes(img, 60)))
    webp = io.BytesIO()
    img.save(webp, "WEBP", quality=80)
    return {
        "resize50": (jpeg_bytes(img.resize((w // 2, h // 2))), "jpg", True),
        "q30": (jpeg_bytes(img, 30), "jpg", True),
        "webp": (webp.getvalue(), "webp", True),
        "twice": (jpeg_bytes(once, 60), "jpg", True),
        "bright": (jpeg_bytes(ImageEnhance.Brightness(img).enhance(1.08)), "jpg", True),
        "crop5": (jpeg_bytes(img.crop((w // 40, h // 40, w - w // 40, h - h // 40))), "jpg", False),
        "rot90": (jpeg_bytes(img.rotate(90, expand=True)), "jpg", False),
    }


# -- videos: Motion-JPEG in QuickTime ---------------------------------------------------------


def _atom(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + kind + body


_MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def write_mov(path: Path, frames: list[bytes], width: int, height: int, fps: int = 10) -> None:
    """A QuickTime movie with one Photo-JPEG video track, one frame per entry of `frames`."""
    scale, delta = 600, 600 // fps
    duration = delta * len(frames)
    ftyp = _atom(b"ftyp", b"qt  ", struct.pack(">I", 0x200), b"qt  ")
    mdat = _atom(b"mdat", *frames)
    data_offset = len(ftyp) + 8
    mvhd = _atom(
        b"mvhd",
        struct.pack(">IIIII", 0, 0, 0, scale, duration)
        + struct.pack(">IH10x", 0x10000, 0x100)
        + _MATRIX
        + bytes(24)
        + struct.pack(">I", 2),
    )
    tkhd = _atom(
        b"tkhd",
        struct.pack(">IIIIIIQHHHH", 0x0000000F, 0, 0, 1, 0, duration, 0, 0, 0, 0, 0)
        + _MATRIX
        + struct.pack(">II", width << 16, height << 16),
    )
    mdhd = _atom(b"mdhd", struct.pack(">IIIIIHH", 0, 0, 0, scale, duration, 0, 0))
    hdlr = _atom(b"hdlr", struct.pack(">I", 0), b"mhlr", b"vide", bytes(12), b"\0")
    vmhd = _atom(b"vmhd", struct.pack(">I", 1), struct.pack(">H3H", 0x40, 0x8000, 0x8000, 0x8000))
    dhlr = _atom(b"hdlr", struct.pack(">I", 0), b"dhlr", b"alis", bytes(12), b"\0")
    dref = _atom(b"dref", struct.pack(">II", 0, 1), _atom(b"alis", struct.pack(">I", 1)))
    dinf = _atom(b"dinf", dref)
    name = b"Photo - JPEG"
    entry_body = (
        bytes(6)
        + struct.pack(">H", 1)  # reserved, data reference index
        + struct.pack(">HH", 0, 0)
        + b"appl"  # version, revision, vendor
        + struct.pack(">II", 0, 0x200)  # temporal, spatial quality
        + struct.pack(">HH", width, height)
        + struct.pack(">II", 72 << 16, 72 << 16)  # resolution
        + struct.pack(">IH", 0, 1)  # data size, frames per sample
        + bytes([len(name)])
        + name
        + bytes(31 - len(name))
        + struct.pack(">hh", 24, -1)  # depth, no colour table
    )
    stsd = _atom(b"stsd", struct.pack(">II", 0, 1), _atom(b"jpeg", entry_body))
    stts = _atom(b"stts", struct.pack(">IIII", 0, 1, len(frames), delta))
    stsc = _atom(b"stsc", struct.pack(">IIIII", 0, 1, 1, len(frames), 1))
    stsz = _atom(
        b"stsz",
        struct.pack(">III", 0, 0, len(frames)),
        *(struct.pack(">I", len(f)) for f in frames),
    )
    stco = _atom(b"stco", struct.pack(">III", 0, 1, data_offset))
    stbl = _atom(b"stbl", stsd, stts, stsc, stsz, stco)
    minf = _atom(b"minf", vmhd, dhlr, dinf, stbl)
    mdia = _atom(b"mdia", mdhd, hdlr, minf)
    trak = _atom(b"trak", tkhd, mdia)
    moov = _atom(b"moov", mvhd, trak)
    path.write_bytes(ftyp + mdat + moov)


def clip_frames(
    seed: int, seconds: int, fps: int = 10, *, quality: int = 85, skip: int = 0
) -> list[bytes]:
    """A moving scene: a base picture that pans, so frames at different times differ."""
    base = base_image(seed).resize((960, 540))
    out = []
    total = seconds * fps
    for i in range(skip, total):
        x = int((i / total) * 300)
        out.append(jpeg_bytes(base.crop((x, 0, x + 640, 360)), quality))
    return out


# -- the fixture set --------------------------------------------------------------------------


def make(out: Path, *, n_images: int = 12) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    must: list[list[str]] = []
    may: list[list[str]] = []
    bases: list[str] = []
    for i in range(n_images):
        img = base_image(1000 + i)
        name = f"img{i:02d}.jpg"
        (out / name).write_bytes(jpeg_bytes(img))
        bases.append(name)
        for tag, (data, ext, required) in variants(img).items():
            vname = f"img{i:02d}_{tag}.{ext}"
            (out / vname).write_bytes(data)
            (must if required else may).append([name, vname])
    videos = {
        "vid_a.mov": clip_frames(1, 12),
        "vid_a_lowq.mov": clip_frames(1, 12, quality=35),
        "vid_a_trim.mov": clip_frames(1, 12, skip=10),  # 1 s shorter at the start
        "vid_b.mov": clip_frames(2, 12),
        "vid_c.mov": clip_frames(3, 6),
    }
    for vname, frames in videos.items():
        write_mov(out / vname, frames, 640, 360)
    must += [["vid_a.mov", "vid_a_lowq.mov"], ["vid_a.mov", "vid_a_trim.mov"]]
    never = [[a, b] for i, a in enumerate(bases) for b in bases[i + 1 :]]
    never += [["vid_a.mov", "vid_b.mov"], ["vid_a.mov", "vid_c.mov"], ["vid_b.mov", "vid_c.mov"]]
    expected = {"must_match": must, "may_match": may, "never_match": never}
    (out / "expected.json").write_text(json.dumps(expected, indent=1))
    return expected


if __name__ == "__main__":
    make(Path(sys.argv[1] if len(sys.argv) > 1 else "build/fixtures"))


# -- greetings and documents (OCR, search, Teach the app) -----------------------------------

_FONTS = {
    "latin": "/System/Library/Fonts/Helvetica.ttc",
    "devanagari": "/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
}


def _font(kind: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(_FONTS[kind], size)
    except OSError:
        return ImageFont.load_default(size)


def greeting(seed: int, text: str, *, script: str = "latin") -> Image.Image:
    """A greeting-like card: a warm gradient, flower shapes and a big line of text."""
    rnd = random.Random(seed)
    w, h = 720, 720
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    top = (rnd.randrange(200, 256), rnd.randrange(120, 200), rnd.randrange(40, 120))
    bottom = (rnd.randrange(150, 230), rnd.randrange(40, 120), rnd.randrange(80, 160))
    for y in range(h):
        t = y / h
        draw.line(
            [(0, y), (w, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)),
        )
    for _ in range(9):  # flowers: a ring of petals round a centre
        cx, cy, r = rnd.randrange(w), rnd.randrange(h // 2, h), rnd.randrange(30, 70)
        petal = (rnd.randrange(200, 256), rnd.randrange(60, 160), rnd.randrange(120, 220))
        for k in range(6):
            a = k * math.tau / 6
            px, py = cx + math.cos(a) * r * 0.8, cy + math.sin(a) * r * 0.8
            draw.ellipse([px - r / 2, py - r / 2, px + r / 2, py + r / 2], fill=petal)
        draw.ellipse([cx - r / 3, cy - r / 3, cx + r / 3, cy + r / 3], fill=(250, 220, 60))
    font = _font(script, 84)
    box = draw.textbbox((0, 0), text, font=font)
    x = (w - (box[2] - box[0])) // 2
    draw.text((x + 3, 123), text, font=font, fill=(60, 20, 20))
    draw.text((x, 120), text, font=font, fill=(255, 255, 240))
    return img


def document(seed: int) -> Image.Image:
    """A document-like page: lines of text and a 12-digit number."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (900, 1200), (250, 250, 246))
    draw = ImageDraw.Draw(img)
    font = _font("latin", 34)
    words = ["account", "invoice", "total", "date", "address", "name", "amount", "payment"]
    for i in range(14):
        line = " ".join(rnd.choice(words) for _ in range(5))
        draw.text((60, 80 + i * 62), line.capitalize(), font=font, fill=(20, 20, 20))
    number = "".join(str(rnd.randrange(10)) for _ in range(12))
    draw.text((60, 1000), f"No. {number[:4]} {number[4:8]} {number[8:]}", font=font, fill=(0, 0, 0))
    return img


def make_text_fixtures(out: Path) -> dict[str, list[str]]:
    """Greetings in English and Hindi and documents; returns the file names by kind."""
    out.mkdir(parents=True, exist_ok=True)
    names: dict[str, list[str]] = {"greeting": [], "greeting_hi": [], "document": []}
    for i, text in enumerate(["Good Morning", "Good Morning!", "Good Morning", "Have a nice day"]):
        name = f"greet_en_{i}.jpg"
        (out / name).write_bytes(jpeg_bytes(greeting(200 + i, text)))
        names["greeting"].append(name)
    for i, text in enumerate(["सुप्रभात", "शुभ प्रभात"]):
        name = f"greet_hi_{i}.jpg"
        (out / name).write_bytes(jpeg_bytes(greeting(300 + i, text, script="devanagari")))
        names["greeting_hi"].append(name)
    for i in range(3):
        name = f"doc_{i}.jpg"
        (out / name).write_bytes(jpeg_bytes(document(400 + i)))
        names["document"].append(name)
    return names
