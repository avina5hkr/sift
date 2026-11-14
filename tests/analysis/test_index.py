"""Stage A (index), stage B (exact copies), candidates, and the Cleanup and media API."""

import io

from PIL import Image

from sift.analysis.index import run_index, screenshot_by_path, screenshot_by_size, thumb_path
from sift.cleanup.candidates import keeper_rank, rebuild_exact, set_selection
from sift.services.plainwords import describe
from sift.services.tasks import TaskRunner
from tests.backup.test_confirm import backed_up, device_id
from tests.backup.test_run import Rig
from tests.helpers import H, client_for

CAM = "/sdcard/DCIM/Camera/IMG_1.jpg"
WA_SENT = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/Sent/IMG-1.jpg"
WA_RECV = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-2.jpg"
SHOT = "/sdcard/DCIM/Screenshots/Screenshot_1.png"


def jpeg(w: int, h: int, *, camera: bool, orientation: int = 1) -> bytes:
    img = Image.new("RGB", (w, h), (200, 30, 30))
    exif = Image.Exif()
    if camera:
        exif[0x010F], exif[0x0110] = "Maker", "Model"
    exif[0x0112] = orientation
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def add_photos(rig: Rig) -> bytes:
    phone = rig.transport.phone
    photo = jpeg(64, 48, camera=True, orientation=6)  # rotated: shown 48 x 64
    for path in (CAM, WA_SENT, WA_RECV):
        phone.add_file(path, photo, mtime=1_700_000_000)
    phone.add_file(SHOT, jpeg(1080, 2400, camera=False), mtime=1_700_000_001)
    return photo


def test_rules() -> None:
    assert screenshot_by_path(["/sdcard/Pictures/Screenshots/a.png"])
    assert screenshot_by_path(["/sdcard/x/Screenshot_2024.png"])
    assert not screenshot_by_path(["/sdcard/DCIM/Camera/a.jpg"])
    assert screenshot_by_size(2400, 1080, [(1080, 2400)])  # either orientation
    assert keeper_rank(CAM, 5) < keeper_rank(WA_RECV, 1) < keeper_rank(WA_SENT, 1)


def test_index_reads_the_backup_copy_and_makes_thumbnails(rig: Rig) -> None:
    add_photos(rig)
    backed_up(rig, ["Camera", "Screenshots", "WhatsApp media"])
    stats = run_index(rig.ctx.db, rig.ctx.paths.cache, [(1080, 2400)])
    assert stats.indexed > 0
    with rig.ctx.db.read() as conn:
        photo = conn.execute(
            "SELECT c.hash, c.width, c.height, c.has_camera_exif, c.is_screenshot, c.unreadable "
            "FROM content c JOIN phone_file p ON p.content_hash = c.hash WHERE p.remote_path = ?",
            (CAM,),
        ).fetchone()
        shot = conn.execute(
            "SELECT c.is_screenshot FROM content c JOIN phone_file p ON p.content_hash = c.hash "
            "WHERE p.remote_path = ?",
            (SHOT,),
        ).fetchone()
        junk = conn.execute(
            "SELECT count(*) FROM content WHERE kind = 'image' AND unreadable = 1"
        ).fetchone()[0]
    assert photo[1:] == (48, 64, 1, 0, 0)  # EXIF orientation applied
    assert shot[0] == 1
    assert junk >= 0
    assert thumb_path(rig.ctx.paths.cache, photo[0]).is_file()
    assert run_index(rig.ctx.db, rig.ctx.paths.cache).indexed == 0  # resumable: nothing left


def test_exact_groups_keep_the_camera_copy_and_selection_survives(rig: Rig) -> None:
    add_photos(rig)
    backed_up(rig, ["Camera", "WhatsApp media"])
    dev = device_id(rig)
    assert rebuild_exact(rig.ctx.db, dev) >= 1
    with rig.ctx.db.read() as conn:
        rows = dict(
            conn.execute(
                "SELECT p.remote_path, c.role FROM candidate c JOIN phone_file p "
                "ON p.id = c.phone_file_id WHERE p.remote_path IN (?, ?, ?)",
                (CAM, WA_SENT, WA_RECV),
            ).fetchall()
        )
        sent_id = conn.execute(
            "SELECT id FROM phone_file WHERE remote_path = ?", (WA_SENT,)
        ).fetchone()[0]
    assert rows == {CAM: "keeper", WA_SENT: "copy", WA_RECV: "copy"}
    set_selection(rig.ctx.db, [sent_id], False)  # the owner unticks one copy
    rebuild_exact(rig.ctx.db, dev)
    with rig.ctx.db.read() as conn:
        assert (
            conn.execute(
                "SELECT checked FROM selection WHERE phone_file_id = ?", (sent_id,)
            ).fetchone()[0]
            == 0
        )


def test_cleanup_and_media_api_end_to_end(rig: Rig) -> None:
    photo = add_photos(rig)
    backed_up(rig, ["Camera", "WhatsApp media"])
    rig.ctx.tasks = TaskRunner(rig.ctx, describe)
    client = client_for(rig.ctx)
    assert client.post("/api/cleanup/analyze", headers=H).status_code == 200
    assert rig.ctx.tasks.wait(30) and rig.ctx.tasks.current().state == "done"
    summary = client.get("/api/cleanup/summary", headers=H).json()
    exact = summary["cards"][0]
    assert summary["has_backup"] and exact["tab"] == "exact" and exact["files"] >= 2
    page = client.get("/api/cleanup/candidates?tab=exact", headers=H).json()
    group = next(g for g in page["groups"] if g["keeper"]["path"] == CAM)
    assert {c["path"] for c in group["copies"]} == {WA_SENT, WA_RECV}
    assert all(c["checked"] for c in group["copies"])
    copy_id, keeper_id = group["copies"][0]["id"], group["keeper"]["id"]
    sel = client.post(
        "/api/selection", headers=H, json={"ids": [copy_id, keeper_id], "checked": False}
    )
    assert sel.json()["changed"] == 1  # a keeper can never be ticked or unticked
    again = client.get("/api/cleanup/candidates?tab=exact", headers=H).json()
    group = next(g for g in again["groups"] if g["keeper"]["path"] == CAM)
    assert [c["checked"] for c in group["copies"] if c["id"] == copy_id] == [False]
    assert (
        client.post(
            "/api/selection", headers=H, json={"all_in_tab": "exact", "checked": True}
        ).json()["changed"]
        >= 2
    )
    # media: thumbnail, preview, the file with Range, the token in the query for <img>
    thumb = client.get(f"/api/media/{copy_id}/thumb?token=t0k")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/media/{copy_id}/preview?max=32", headers=H).status_code == 200
    part = client.get(f"/api/media/{copy_id}/file", headers={**H, "Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == photo[:10]
    assert client.head(f"/api/media/{copy_id}/thumb", headers=H).status_code == 200
    assert client.get(f"/api/media/{copy_id}/thumb").status_code == 401
    assert client.get(f"/api/media/{copy_id}/thumb?token=wrong").status_code == 401
    assert client.get("/api/media/999999/thumb", headers=H).status_code == 404
    assert client.get("/api/media/..%2F..%2Fetc/thumb", headers=H).status_code in (404, 422)


def test_a_keeper_gone_from_the_phone_never_breaks_review(rig: Rig) -> None:
    add_photos(rig)
    backed_up(rig, ["Camera", "WhatsApp media"])
    rig.ctx.tasks = TaskRunner(rig.ctx, describe)
    client = client_for(rig.ctx)
    client.post("/api/cleanup/analyze", headers=H)
    assert rig.ctx.tasks.wait(30)
    rig.transport.phone.modify(CAM, b"changed bytes", 1_700_000_100)  # the keeper changed
    rig.transport.phone.remove(WA_RECV)
    backed_up(rig, ["Camera", "WhatsApp media"])  # the backup rebuilds the groups
    page = client.get("/api/cleanup/candidates?tab=exact", headers=H)
    assert page.status_code == 200
    paths = {c["path"] for g in page.json()["groups"] for c in [g["keeper"], *g["copies"]]}
    assert WA_RECV not in paths and CAM not in paths  # nothing stale; SENT has no copy left
    card = client.get("/api/cleanup/summary", headers=H).json()["cards"][0]
    assert card["files"] == sum(len(g["copies"]) for g in page.json()["groups"])


def test_concurrent_thumbnail_requests_for_one_content_all_succeed(rig: Rig) -> None:
    from concurrent.futures import ThreadPoolExecutor

    add_photos(rig)
    backed_up(rig, ["Camera", "WhatsApp media"])
    client = client_for(rig.ctx)
    with rig.ctx.db.read() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM phone_file WHERE remote_path IN (?, ?, ?)", (CAM, WA_SENT, WA_RECV)
            )
        ]
    with ThreadPoolExecutor(8) as pool:
        codes = list(
            pool.map(lambda i: client.get(f"/api/media/{i}/thumb", headers=H).status_code, ids * 10)
        )
    assert set(codes) == {200}


def test_media_needs_the_host_and_never_takes_the_query_token_elsewhere(rig: Rig) -> None:
    add_photos(rig)
    backed_up(rig, ["Camera"])
    client = client_for(rig.ctx)
    with rig.ctx.db.read() as conn:
        pid = conn.execute("SELECT id FROM phone_file WHERE remote_path = ?", (CAM,)).fetchone()[0]
    assert client.get(
        f"/api/media/{pid}/thumb", headers={**H, "Host": "evil.example"}
    ).status_code in (400, 403)
    assert client.get("/api/cleanup/summary?token=t0k").status_code == 401
    assert client.head(f"/api/media/{pid}/thumb?token=t0k").status_code == 401  # HEAD: header only


def test_big_jpegs_render_small_and_report_their_true_size(tmp_path) -> None:
    from sift.analysis.index import render_jpeg

    src = tmp_path / "big.jpg"
    Image.new("RGB", (6000, 4000), (1, 2, 3)).save(src, "JPEG")
    w, h, fmt, _hashes = render_jpeg(src, tmp_path / "out.jpg", 256)
    assert (w, h, fmt) == (6000, 4000, "JPEG")
    with Image.open(tmp_path / "out.jpg") as out:
        assert max(out.size) <= 256


def test_a_drive_not_found_by_uuid_is_never_read(rig: Rig, tmp_path) -> None:
    from sift.backup.replicator import Dest, Replicator
    from tests.backup.test_run import FakeSsd

    add_photos(rig)
    backed_up(rig, ["Camera"])
    ssd = FakeSsd(rig, tmp_path / "ssd" / "backup")
    from sift.backup.run import mac_target

    mac = mac_target(rig.ctx.db, rig.ctx.config)
    Replicator(rig.ctx.db).replicate(
        Dest(mac.dest_id, "mac", mac.root),
        Dest(ssd._on.dest_id, "ssd", tmp_path / "ssd" / "backup"),
    )
    import shutil

    shutil.rmtree(mac.root)  # only the SSD copy is left, and that SSD is not plugged in
    ssd.connected = False
    rig.ctx.ssd = ssd  # type: ignore[assignment]
    with rig.ctx.db.read() as conn:
        pid = conn.execute("SELECT id FROM phone_file WHERE remote_path = ?", (CAM,)).fetchone()[0]
    assert client_for(rig.ctx).get(f"/api/media/{pid}/thumb", headers=H).status_code == 404


def test_heic_photos_are_decoded() -> None:
    """Phone cameras save HEIC; Sift reads them through pi-heif (decode only). The fixture was
    made with macOS `sips`, since pi-heif has no encoder."""
    from pathlib import Path

    import sift.analysis.index  # noqa: F401  (registers the HEIC opener)

    with Image.open(Path(__file__).parent / "fixtures" / "sample.heic") as im:
        assert im.format == "HEIF" and im.size == (160, 120)
        r, _g, b = im.convert("RGB").getpixel((80, 60))  # type: ignore[misc]
        assert r > 200 and b < 100  # the yellow square in the middle
