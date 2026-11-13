"""Stage C: 64-bit perceptual hashes (spec 8). Stored signed, compared by Hamming distance.

dHash: the image in grey at 9x8; bit = left pixel brighter than its right neighbour.
pHash: grey at 32x32, 2-D DCT-II, the top-left 8x8 block, bit = above that block's median
(the DC term included in the block, excluded from the median).
"""

import math
from functools import cache

from PIL import Image

MASK = (1 << 64) - 1


def to_signed(value: int) -> int:
    """SQLite stores signed 64-bit integers."""
    return value - (1 << 64) if value >= 1 << 63 else value


def to_unsigned(value: int) -> int:
    return value & MASK


def distance(a: int, b: int) -> int:
    return (to_unsigned(a) ^ to_unsigned(b)).bit_count()


def dhash(img: Image.Image) -> int:
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(small.get_flattened_data())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


@cache
def _cosines() -> tuple[tuple[float, ...], ...]:
    """cos((2x + 1) u π / 64) for u, x in 0..31 (only u < 8 is used)."""
    return tuple(
        tuple(math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)) for u in range(8)
    )


def phash(img: Image.Image) -> int:
    small = img.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    px = list(small.get_flattened_data())
    cos = _cosines()
    # rows first: 32 rows x 8 frequencies, then columns: 8 x 8
    rows = [
        [sum(px[y * 32 + x] * cos[u][x] for x in range(32)) for u in range(8)] for y in range(32)
    ]
    block = [[sum(rows[y][u] * cos[v][y] for y in range(32)) for u in range(8)] for v in range(8)]
    flat = [block[v][u] for v in range(8) for u in range(8)]
    median = sorted(flat[1:])[len(flat[1:]) // 2]
    bits = 0
    for value in flat:
        bits = (bits << 1) | (value > median)
    return bits


def hashes(img: Image.Image) -> tuple[int, int]:
    """(dhash, phash) as signed 64-bit integers, ready for the database."""
    return to_signed(dhash(img)), to_signed(phash(img))
