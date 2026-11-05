from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StrictBool

from sift.device.adb_manager import AdbDownloadError, AdbError, AdbLocation, AdbManager
from sift.server.deps import Ctx
from sift.server.errors import ApiError

router = APIRouter()


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
def download_adb(manager: AdbManagerDep, body: DownloadRequest | None = None) -> AdbLocationOut:
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
    try:
        return AdbLocationOut.of(manager.download(consent=True))
    except AdbDownloadError as e:
        raise ApiError(502, "adb_download_failed", e.message) from e
    except AdbError as e:
        raise ApiError(500, "adb_error", e.message) from e
