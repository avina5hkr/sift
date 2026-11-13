"""OCR, the model download, embeddings, clusters, search and labels (M6)."""

import hashlib
import io
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sift.analysis.cluster import find_clusters
from sift.analysis.embed import ClipOnnx, l2
from sift.analysis.models import (
    CLIP,
    ConsentNeeded,
    ModelDownloadError,
    ModelFile,
    ModelSpec,
    download,
    installed,
)
from sift.analysis.ocr import recognize
from sift.core.config import AnalysisConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import make_fixtures

REAL_MODELS = Path(
    os.environ.get("SIFT_TEST_MODELS", Path(__file__).resolve().parents[2] / "build/models")
)


class FakeModel:
    """Deterministic stand-in: an image is its 8x8 grey thumbnail; text "warm" is a warm image."""

    dim = 64
    version = 1

    def encode_image(self, images):
        rows = [
            np.asarray(i.convert("L").resize((8, 8)), dtype=np.float32).ravel() - 128
            for i in images
        ]
        return l2(np.stack(rows))

    def encode_text(self, texts):
        return self.encode_image([make_fixtures.greeting(200, "Good Morning") for _ in texts])


@pytest.fixture(scope="module")
def text_fixtures(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, list[str]]]:
    out = tmp_path_factory.mktemp("text")
    return out, make_fixtures.make_text_fixtures(out)


def test_ocr_reads_english_and_hindi(text_fixtures) -> None:
    folder, names = text_fixtures
    en = recognize(folder / names["greeting"][0])
    assert en is not None and "good morning" in en.text and en.density > 0
    hi = recognize(folder / names["greeting_hi"][1])
    assert hi is not None and "शुभ" in hi.text  # Hindi is read (D1)


def test_clusters_split_join_and_medoid() -> None:
    rng = np.random.default_rng(1)
    a, b = l2(rng.normal(size=(1, 32))), l2(rng.normal(size=(1, 32)))
    rows = np.vstack(
        [
            l2(a + 0.05 * rng.normal(size=(5, 32))),
            l2(b + 0.05 * rng.normal(size=(4, 32))),
            l2(rng.normal(size=(1, 32))),
        ]
    )
    hashes = [f"h{i}" for i in range(len(rows))]
    clusters = find_clusters(hashes, rows.astype(np.float32), AnalysisConfig())
    assert [len(c.members) for c in clusters] == [5, 4]  # largest first, singleton dropped
    assert clusters[0].members[0] == clusters[0].medoid and clusters[0].sims[0] == pytest.approx(
        1, abs=1e-5
    )


def _spec(tmp: Path, data: bytes, *, good: bool = True) -> ModelSpec:
    digest = hashlib.sha256(data if good else b"other").hexdigest()
    return ModelSpec("m", 1, (ModelFile("f.bin", "https://example.invalid/f", digest, len(data)),))


def test_model_download_needs_consent_and_checks_the_hash(tmp_path: Path) -> None:
    data = b"model bytes" * 100
    opener = lambda _url: io.BytesIO(data)  # noqa: E731
    with pytest.raises(ConsentNeeded):
        download(tmp_path, consent=False, spec=_spec(tmp_path, data), opener=opener)
    with pytest.raises(ModelDownloadError):
        download(tmp_path, consent=True, spec=_spec(tmp_path, data, good=False), opener=opener)
    assert not list((tmp_path / "m").iterdir())  # the wrong file was discarded
    download(tmp_path, consent=True, spec=_spec(tmp_path, data), opener=opener)
    assert installed(tmp_path, _spec(tmp_path, data)) and not installed(tmp_path)
    assert CLIP.size > 100_000_000  # the real model's size is shown before the download


def test_teach_the_app_and_search_over_the_api(rig) -> None:
    from sift.server.routes.teach import set_model
    from sift.services.plainwords import describe
    from sift.services.tasks import TaskRunner
    from tests.backup.test_confirm import backed_up
    from tests.helpers import H, client_for

    base = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/"
    phone = rig.transport.phone
    for i in range(4):  # four near-identical greeting cards forwarded on WhatsApp
        img = make_fixtures.greeting(200, "Good Morning").rotate(i * 0.5)
        phone.add_file(
            f"{base}IMG-G{i}.jpg",
            make_fixtures.jpeg_bytes(img, 80 - i * 5),
            mtime=1_700_000_000 + i,
        )
    phone.add_file(
        f"{base}IMG-DOC.jpg",
        make_fixtures.jpeg_bytes(make_fixtures.document(1)),
        mtime=1_700_000_100,
    )
    backed_up(rig, ["WhatsApp media"])
    set_model(rig.ctx, FakeModel())
    try:
        rig.ctx.tasks = TaskRunner(rig.ctx, describe)
        client = client_for(rig.ctx)
        client.post("/api/cleanup/analyze", headers=H)
        assert rig.ctx.tasks.wait(120) and rig.ctx.tasks.current().state == "done", (
            rig.ctx.tasks.current()
        )
        result = rig.ctx.tasks.current().result
        assert result["ocr"] >= 4 and result["embedded"] >= 5 and result["clusters"] >= 1
        body = client.get("/api/cleanup/clusters", headers=H).json()
        card = body["clusters"][0]
        assert card["size"] >= 4 and "good morning" in (card["sample_text"] or "")
        members = client.get(f"/api/cleanup/clusters/{card['id']}", headers=H).json()
        assert len(members) == card["size"]
        assert (
            client.post(
                "/api/labels",
                headers=H,
                json={"cluster_id": card["id"], "label": "junk", "tag": "greeting"},
            ).json()["labelled"]
            >= 4
        )
        assert (
            client.get("/api/cleanup/clusters", headers=H).json()["clusters"][0]["label"] == "junk"
        )
        hits = client.get("/api/cleanup/search?q=good%20morning", headers=H).json()
        assert hits and hits[0]["path"].startswith(base + "IMG-G")
    finally:
        set_model(rig.ctx, None)
    assert (
        client_for(rig.ctx).get("/api/cleanup/search?q=x", headers=H).status_code == 409
    )  # no model


@pytest.mark.slow
@pytest.mark.skipif(
    not (REAL_MODELS / "vision_model_quantized.onnx").is_file(), reason="model files not present"
)
def test_the_real_model_finds_good_morning_cards(text_fixtures, tmp_path: Path) -> None:
    folder, names = text_fixtures
    models = tmp_path / "models"
    (models / CLIP.key).mkdir(parents=True)
    for f in CLIP.files:
        (models / CLIP.key / f.name).symlink_to(REAL_MODELS / f.name)
    model = ClipOnnx(models)
    pictures = make_fixtures.make(tmp_path / "pics", n_images=6)
    del pictures
    files = {n: folder / n for kind in names for n in names[kind]}
    files |= {p.name: p for p in (tmp_path / "pics").glob("img0?.jpg")}
    keys = list(files)
    vectors = model.encode_image([Image.open(files[k]) for k in keys])
    q = model.encode_text(["good morning"])[0]
    ranked = [keys[i] for i in np.argsort(-(vectors @ q))]
    english = set(names["greeting"]) - {"greet_en_3.jpg"}  # "Have a nice day" is not "good morning"
    assert set(ranked[: len(english)]) == english, ranked
