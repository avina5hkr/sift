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


def test_near_and_similar_tabs_over_the_api(rig) -> None:
    import io

    from PIL import Image

    from sift.services.plainwords import describe
    from sift.services.tasks import TaskRunner
    from tests.backup.test_confirm import backed_up
    from tests.helpers import H, client_for

    def jpeg(img: Image.Image, q: int = 90, *, camera: bool = False) -> bytes:
        out, exif = io.BytesIO(), Image.Exif()
        if camera:
            exif[0x010F] = "Maker"
        img.save(out, "JPEG", quality=q, exif=exif.tobytes())
        return out.getvalue()

    base = make_fixtures.base_image(7)
    burst = make_fixtures.base_image(8)
    phone = rig.transport.phone
    cam = "/sdcard/DCIM/Camera/IMG_7.jpg"
    wa = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-7-WA.jpg"
    phone.add_file(cam, jpeg(base, camera=True), mtime=1_700_000_000)
    phone.add_file(wa, jpeg(base.resize((320, 240)), 40), mtime=1_700_000_500)
    phone.add_file(
        "/sdcard/DCIM/Camera/IMG_8.jpg", jpeg(base, 70, camera=True), mtime=1_700_000_001
    )
    phone.add_file("/sdcard/DCIM/Camera/IMG_9.jpg", jpeg(burst, camera=True), mtime=1_700_000_002)
    phone.add_file(
        "/sdcard/DCIM/Camera/IMG_10.jpg", jpeg(burst, 60, camera=True), mtime=1_700_000_003
    )
    backed_up(rig, ["Camera", "WhatsApp media"])
    rig.ctx.tasks = TaskRunner(rig.ctx, describe)
    client = client_for(rig.ctx)
    client.post("/api/cleanup/analyze", headers=H)
    assert rig.ctx.tasks.wait(60) and rig.ctx.tasks.current().state == "done"
    near = client.get("/api/cleanup/candidates?tab=near", headers=H).json()["groups"]
    group = next(g for g in near if any(c["path"] == wa for c in g["copies"]))
    assert group["keeper"]["path"].startswith("/sdcard/DCIM/Camera/")  # the camera photo is kept
    similar = client.get("/api/cleanup/candidates?tab=similar", headers=H).json()["groups"]
    assert similar and not any(
        c["checked"] for g in similar for c in g["copies"]
    )  # never pre-ticked
    wa_id = next(c["id"] for c in group["copies"] if c["path"] == wa)
    assert (
        client.post("/api/selection", headers=H, json={"ids": [wa_id], "checked": True}).json()[
            "changed"
        ]
        == 1
    )
    keeper_id = group["keeper"]["id"]
    assert (
        client.post("/api/selection", headers=H, json={"ids": [keeper_id], "checked": True}).json()[
            "changed"
        ]
        == 0
    )
    cards = {c["tab"]: c for c in client.get("/api/cleanup/summary", headers=H).json()["cards"]}
    assert cards["near"]["files"] >= 1 and cards["similar"]["groups"] >= 1
