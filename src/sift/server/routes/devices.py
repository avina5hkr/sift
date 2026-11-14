import threading
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, StrictBool

from sift.device.adb_manager import AdbDownloadError, AdbError, AdbLocation, AdbManager
from sift.server.deps import Ctx
from sift.server.errors import ApiError

router = APIRouter()
_download_running = threading.Lock()  # one download request at a time; a second is refused


def get_adb_manager(ctx: Ctx) -> AdbManager:
    """The adb manager for this app; tests override it so nothing is ever downloaded."""
    return AdbManager(ctx.paths, ctx.config.phone)


AdbManagerDep = Annotated[AdbManager, Depends(get_adb_manager)]


class DownloadRequest(BaseModel):
    consent: StrictBool = False


class AdbLocationOut(BaseModel):
    path: str | None
    version: str | None
    status: str
    message: str

    @classmethod
    def of(cls, loc: AdbLocation) -> AdbLocationOut:
        path: Path | None = loc.path
        return cls(
            path=str(path) if path else None,
            version=loc.version,
            status=str(loc.status),
            message=loc.message,
        )


@router.post("/devices/adb/download")
def download_adb(
    ctx: Ctx, manager: AdbManagerDep, body: DownloadRequest | None = None
) -> AdbLocationOut:
    """Download Google's platform-tools. The only network call in the app besides other
    first-run downloads the person approves; it needs `{"consent": true}`."""
    consent = body.consent if body else False
    if not consent:
        raise ApiError(
            400,
            "consent_required",
            "Sift downloads Google's platform-tools only with your permission. "
            "Approve the download to continue.",
        )
    if not _download_running.acquire(blocking=False):
        raise ApiError(
            409, "download_running", "adb is already being downloaded. Wait for it to finish."
        )
    try:
        loc = AdbLocationOut.of(manager.download(consent=True))
    except AdbDownloadError as e:
        raise ApiError(502, "adb_download_failed", e.message) from e
    except AdbError as e:
        raise ApiError(500, "adb_error", e.message) from e
    finally:
        _download_running.release()
    if ctx.tracker is not None:
        ctx.tracker.retry()  # adb is there now: look for the phone at once, not after a backoff
    return loc


class StoredDevice(BaseModel):
    uuid: str
    serial: str | None
    model: str | None
    android_version: str | None
    last_seen_at: int | None


@router.get("/devices")
def list_devices(ctx: Ctx) -> list[StoredDevice]:
    """The phones Sift has stored. Read-only; nothing else about a phone leaves the database."""
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT uuid, serial, model, android_version, last_seen_at FROM device ORDER BY id"
        ).fetchall()
    return [
        StoredDevice(
            uuid=row[0], serial=row[1], model=row[2], android_version=row[3], last_seen_at=row[4]
        )
        for row in rows
    ]


@router.post("/devices/retry", status_code=204)
def retry_device(ctx: Ctx) -> Response:
    """Look for the phone again now (reopen the device list, read the phone afresh)."""
    if ctx.tracker is None:
        raise ApiError(409, "no_tracker", "Sift is not looking for a phone right now.")
    ctx.tracker.retry()
    return Response(status_code=204)


# -- Wi-Fi (spec 6) ------------------------------------------------------------------------


class WifiPair(BaseModel):
    address: str
    code: str


class WifiConnect(BaseModel):
    address: str


class WifiForget(BaseModel):
    name: str
    address: str


def _adb_port(ctx: Ctx) -> int:
    return ctx.config.phone.adb_server_port


@router.get("/phone/wifi")
def wifi_status(ctx: Ctx) -> dict[str, object]:
    """Remembered phones and the ones announcing Wireless debugging on this network now."""
    from dataclasses import asdict

    from sift.device import wifi

    return {
        "remembered": [asdict(r) for r in wifi.remembered(ctx.db)],
        "found": [asdict(s) for s in wifi.services(_adb_port(ctx))],
        "allow_delete": ctx.config.phone.allow_delete_over_wifi,
    }


@router.post("/phone/wifi/pair")
def wifi_pair(ctx: Ctx, body: WifiPair) -> dict[str, str | None]:
    from sift.device import wifi

    try:
        return wifi.pair_and_connect(ctx.db, _adb_port(ctx), body.address, body.code)
    except wifi.WifiError as e:
        raise ApiError(409, "wifi", str(e)) from e


@router.post("/phone/wifi/connect")
def wifi_connect(ctx: Ctx, body: WifiConnect) -> dict[str, str]:
    from sift.device import wifi

    try:
        address = wifi.check_address(body.address)
        answer = wifi.connect(_adb_port(ctx), address)
    except wifi.WifiError as e:
        raise ApiError(409, "wifi", str(e)) from e
    name = next((s.name for s in wifi.services(_adb_port(ctx)) if s.address == address), "")
    wifi.remember(ctx.db, name, address, connected=True)
    return {"connected": address, "answer": answer}


@router.post("/phone/wifi/forget", status_code=204)
def wifi_forget(ctx: Ctx, body: WifiForget) -> Response:
    from sift.device import wifi

    wifi.forget(ctx.db, body.name, body.address)
    return Response(status_code=204)
