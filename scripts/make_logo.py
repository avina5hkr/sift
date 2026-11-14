"""Draw Sift's logo from one set of shapes: SVGs for the web and docs, and the macOS app icon.

The mark is an inverted pyramid of photo tiles (three, then two, then one gold tile with a
check): sift through many, keep what matters, verified. On a teal-to-indigo rounded square, on
Apple's 1024 icon grid (an 824 px body with a soft shadow).

    uv run python scripts/make_logo.py      # or: make logo

Writes docs/assets/logo.svg (the app icon), web/public/favicon.svg (the mark, edge to edge),
packaging/Sift.icns (through macOS `iconutil`), and docs/assets/social-preview.png (1280x640,
for the repository's social preview in GitHub settings).
"""

import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]

CANVAS = 1024
BODY = (100, 100, 924, 924)  # Apple's icon grid: the body, inside the 1024 canvas
BODY_RADIUS = 185
GRADIENT = ((0.0, "#2DD4BF"), (0.5, "#0EA5E9"), (1.0, "#4338CA"))  # teal, sky, indigo
TILE, TILE_RADIUS = 150, 34
ROWS = (  # (x, y, opacity) of the white photo tiles
    (247, 252, 0.55), (437, 252, 0.55), (627, 252, 0.55),
    (342, 432, 0.85), (532, 432, 0.85),
)  # fmt: skip
KEEPER = (424, 608, 176, 42)  # x, y, size, radius of the gold tile
KEEPER_GRADIENT = ("#FDE68A", "#F59E0B")
CHECK = ((470, 697), (502, 729), (555, 664))
CHECK_WIDTH = 24


def _hex(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


# -- SVG -----------------------------------------------------------------------------------


def svg(*, full_bleed: bool) -> str:
    """The icon as SVG. `full_bleed`: the body fills the view and there is no shadow (favicon)."""
    x0, y0, x1, y1 = BODY
    view = f"{x0} {y0} {x1 - x0} {y1 - y0}" if full_bleed else f"0 0 {CANVAS} {CANVAS}"
    stops = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in GRADIENT)
    shadow = (
        ""
        if full_bleed
        else '<filter id="shadow" x="-10%" y="-10%" width="120%" height="125%">'
        '<feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#0B1020" '
        'flood-opacity="0.30"/></filter>'
    )
    body_filter = "" if full_bleed else ' filter="url(#shadow)"'
    tiles = "".join(
        f'<rect x="{x}" y="{y}" width="{TILE}" height="{TILE}" rx="{TILE_RADIUS}" '
        f'fill="#FFFFFF" fill-opacity="{a}"/>'
        for x, y, a in ROWS
    )
    kx, ky, ks, kr = KEEPER
    check = " ".join(f"{px},{py}" for px, py in CHECK)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" role="img" '
        f'aria-label="Sift">'
        f"<title>Sift</title>"
        f"<defs>"
        f'<linearGradient id="body" x1="0" y1="0" x2="1" y2="1">{stops}</linearGradient>'
        f'<linearGradient id="shine" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="#FFFFFF" stop-opacity="0.22"/>'
        f'<stop offset="0.5" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>'
        f'<linearGradient id="keeper" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{KEEPER_GRADIENT[0]}"/>'
        f'<stop offset="1" stop-color="{KEEPER_GRADIENT[1]}"/></linearGradient>'
        f"{shadow}</defs>"
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{BODY_RADIUS}" '
        f'fill="url(#body)"{body_filter}/>'
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{BODY_RADIUS}" '
        f'fill="url(#shine)"/>'
        f"{tiles}"
        f'<rect x="{kx}" y="{ky}" width="{ks}" height="{ks}" rx="{kr}" fill="url(#keeper)"/>'
        f'<polyline points="{check}" fill="none" stroke="#FFFFFF" stroke-width="{CHECK_WIDTH}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f"</svg>\n"
    )


# -- raster (drawn at 4x, then reduced) ----------------------------------------------------


def _gradient(size: int, stops: tuple[tuple[float, str], ...], diagonal: bool) -> Image.Image:
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64) / (size - 1)
    t = (xx + yy) / 2 if diagonal else yy
    out = np.zeros((size, size, 3))
    offsets = [o for o, _ in stops]
    for ch in range(3):
        out[..., ch] = np.interp(t, offsets, [_hex(c)[ch] for _, c in stops])
    return Image.fromarray(out.astype(np.uint8), "RGB").convert("RGBA")


def _mask(size: int, box: tuple[float, ...], radius: float, scale: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([v * scale for v in box], radius=radius * scale, fill=255)
    return m


def raster(size: int = CANVAS, ss: int = 4) -> Image.Image:
    """The full app icon (with its shadow) as an RGBA image of `size` pixels."""
    n = CANVAS * ss
    s = float(ss)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    body = _mask(n, BODY, BODY_RADIUS, s)
    shadow = Image.new("RGBA", (n, n), (11, 16, 32, 0))
    shadow.putalpha(
        ImageChops.offset(body, 0, int(12 * s))
        .filter(ImageFilter.GaussianBlur(14 * s))
        .point(lambda v: int(v * 0.30))
    )
    img.alpha_composite(shadow)

    fill = _gradient(n, GRADIENT, diagonal=True)
    fill.putalpha(body)
    img.alpha_composite(fill)
    shine = _gradient(n, ((0.0, "#FFFFFF"), (0.5, "#FFFFFF")), diagonal=False)
    yy = np.linspace(0.22, 0.0, n // 2)
    alpha = np.zeros(n)
    alpha[: n // 2] = yy
    shine_alpha = (np.array(body, dtype=np.float64) * alpha[:, None]).astype(np.uint8)
    shine.putalpha(Image.fromarray(shine_alpha, "L"))
    img.alpha_composite(shine)

    for x, y, a in ROWS:
        tile = Image.new("RGBA", (n, n), (255, 255, 255, 0))
        tile.putalpha(
            _mask(n, (x, y, x + TILE, y + TILE), TILE_RADIUS, s).point(lambda v, a=a: int(v * a))
        )
        img.alpha_composite(tile)

    kx, ky, ks, kr = KEEPER
    stops = ((ky / CANVAS, KEEPER_GRADIENT[0]), ((ky + ks) / CANVAS, KEEPER_GRADIENT[1]))
    keeper = _gradient(n, stops, diagonal=False)
    keeper.putalpha(_mask(n, (kx, ky, kx + ks, ky + ks), kr, s))
    img.alpha_composite(keeper)

    draw = ImageDraw.Draw(img)
    pts = [(px * s, py * s) for px, py in CHECK]
    w = int(CHECK_WIDTH * s)
    draw.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")
    for px, py in (pts[0], pts[-1]):  # round caps
        draw.ellipse([px - w / 2, py - w / 2, px + w / 2, py + w / 2], fill=(255, 255, 255, 255))

    return img.resize((size, size), Image.Resampling.LANCZOS)


FONT = Path("/System/Library/Fonts/SFNS.ttf")  # San Francisco, part of macOS


def social_preview(out: Path) -> None:
    """1280x640: the icon, the name and one line on a dark background."""
    from PIL import ImageFont

    w, h = 1280, 640
    card = _gradient(w, ((0.0, "#0B1020"), (1.0, "#1E1B4B")), diagonal=True).resize((w, h))
    card.alpha_composite(raster(360), (110, 140))

    def font(size: int, style: bytes) -> ImageFont.FreeTypeFont:
        f = ImageFont.truetype(str(FONT), size)
        with contextlib.suppress(OSError, ValueError):  # not a variable font: default style
            f.set_variation_by_name(style)  # a named instance: Regular, Semibold, Bold
        return f

    draw = ImageDraw.Draw(card)
    draw.text((530, 200), "Sift", font=font(150, b"Bold"), fill="#FFFFFF")
    draw.text((534, 390), "Back up your Android phone to your Mac,", font=font(40, b"Regular"),
              fill="#C7D2FE")  # fmt: skip
    draw.text((534, 442), "then free space safely. Local only.", font=font(40, b"Regular"),
              fill="#C7D2FE")  # fmt: skip
    card.convert("RGB").save(out, optimize=True)


def icns(out: Path) -> None:
    big = raster(CANVAS)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Sift.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = points * scale
                name = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
                (big if px == CANVAS else big.resize((px, px), Image.Resampling.LANCZOS)).save(
                    iconset / name
                )
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)


def main() -> None:
    (ROOT / "docs" / "assets").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "assets" / "logo.svg").write_text(svg(full_bleed=False))
    (ROOT / "web" / "public" / "favicon.svg").write_text(svg(full_bleed=True))
    if FONT.exists():
        social_preview(ROOT / "docs" / "assets" / "social-preview.png")
    if shutil.which("iconutil"):
        icns(ROOT / "packaging" / "Sift.icns")
    else:  # pragma: no cover - iconutil is part of macOS
        print("iconutil not found: packaging/Sift.icns not updated")
    print("Wrote docs/assets/logo.svg, web/public/favicon.svg, packaging/Sift.icns, "
          "docs/assets/social-preview.png")  # fmt: skip


if __name__ == "__main__":
    main()
