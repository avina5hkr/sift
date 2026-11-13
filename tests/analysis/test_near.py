"""Fixture accuracy (M5 exit): variants group with their original, different images never do."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from sift.analysis.index import render_jpeg
from sift.analysis.near import Item, bands, find_groups, group_type, keeper_score
from sift.analysis.phash import distance
from sift.analysis.video import probe
from sift.core.config import AnalysisConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import make_fixtures


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, list[list[str]]]]:
    out = tmp_path_factory.mktemp("fixtures")
    expected = make_fixtures.make(out)
    return out, expected  # type: ignore[return-value]


def items_for(folder: Path, cache: Path) -> dict[str, Item]:
    found: dict[str, Item] = {}
    for path in sorted(folder.iterdir()):
        if path.suffix == ".json":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix == ".mov":
            info = probe(path)
            assert info is not None, path.name
            found[path.name] = Item(
                digest,
                "video",
                info.width,
                info.height,
                path.stat().st_size,
                info.frames[2],
                None,
                False,
                info.duration_ms,
                info.frames,
            )
        else:
            w, h, _fmt, hashes = render_jpeg(path, cache / f"{digest}.jpg", 256, with_hashes=True)
            assert hashes is not None
            found[path.name] = Item(
                digest, "image", w, h, path.stat().st_size, hashes[0], hashes[1], False
            )
    return found


def test_fixture_accuracy(fixtures, tmp_path: Path) -> None:
    folder, expected = fixtures
    items = items_for(folder, tmp_path)
    groups = find_groups(list(items.values()), AnalysisConfig())
    group_of: dict[str, int] = {}
    for n, g in enumerate(groups):
        for digest in (g.keeper, *g.copies):
            group_of[digest] = n

    def together(a: str, b: str) -> bool:
        ga, gb = group_of.get(items[a].hash), group_of.get(items[b].hash)
        return ga is not None and ga == gb

    must = expected["must_match"]
    hits = sum(together(a, b) for a, b in must)
    misses = [(a, b) for a, b in must if not together(a, b)]
    assert hits / len(must) >= 0.98, misses
    wrong = [(a, b) for a, b in expected["never_match"] if together(a, b)]
    assert wrong == []
    json.dumps(misses)  # (kept for the report below when it fails)


def test_bands_pigeonhole() -> None:
    a = 0x0123456789ABCDEF
    b = a ^ 0b1011_0000_0001_0000_0000_0010_0000_0000_0100_0000_0000_1000_0000_0001_0000_0000
    assert distance(a, b) <= 8
    assert set(bands(a)) & set(bands(b))  # within distance 8: at least one band is equal


def test_group_types_and_keeper() -> None:
    cam = Item("c", "image", 4000, 3000, 5_000_000, 0, 0, True, origins={"camera"}, mtime=5)
    sent = Item("s", "image", 1600, 1200, 300_000, 0, 0, False, origins={"whatsapp_sent"}, mtime=9)
    recv = Item("r", "image", 1600, 1200, 300_000, 0, 0, False, origins={"whatsapp_received"})
    burst = Item("b", "image", 4000, 3000, 4_900_000, 0, 0, True, origins={"camera"}, mtime=6)
    assert group_type([sent, recv]) == "wa_forward"
    assert group_type([cam, sent]) == "mixed"  # a Sent copy of a camera photo
    assert group_type([cam, burst]) == "camera_burst"
    assert max([sent, cam, recv], key=keeper_score) is cam
