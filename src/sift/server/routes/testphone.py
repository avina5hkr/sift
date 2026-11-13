"""Test-only control of the fake phone, for the browser tests (`web/e2e`).

Mounted only when the test hook SIFT_TEST_PHONE_CONTROL=1 is on (see `sift.core.testhooks`: a
packaged app ignores it) and only when the fake phone runs (`--fake-phone`, which needs
`--no-window` and SIFT_HOME). Every route needs the session token like the rest of the API.
"""

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from sift.device.fake_tracker import FakePhoneControl
from sift.server.deps import Ctx
from sift.server.errors import ApiError

router = APIRouter(prefix="/test/phone")


def _control(ctx: Ctx) -> FakePhoneControl:
    if ctx.phone_control is None:
        raise ApiError(409, "no_fake_phone", "The fake phone is not running.")
    return ctx.phone_control


@router.post("/unplug", status_code=204)
def unplug(ctx: Ctx) -> Response:
    _control(ctx).unplug()
    return Response(status_code=204)


@router.post("/replug", status_code=204)
def replug(ctx: Ctx) -> Response:
    _control(ctx).replug()
    return Response(status_code=204)


class SpeedRequest(BaseModel):
    bytes_per_s: int | None = Field(default=None, gt=0)  # null: full speed


@router.post("/speed", status_code=204)
def speed(ctx: Ctx, body: SpeedRequest) -> Response:
    _control(ctx).set_speed(body.bytes_per_s)
    return Response(status_code=204)


class RemoveRequest(BaseModel):
    path: str
    keep_folder: bool = False  # empty the folder instead of removing it


class Removed(BaseModel):
    files: int


@router.post("/remove")
def remove(ctx: Ctx, body: RemoveRequest) -> Removed:
    """Delete a file, or a folder and all it holds (or just what it holds), from the fake phone."""
    try:
        return Removed(files=_control(ctx).remove(body.path, keep_folder=body.keep_folder))
    except KeyError:
        raise ApiError(404, "no_such_path", "That path is not on the fake phone.") from None


class ImageRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=20)  # the same small photo at each path
    width: int = Field(default=64, ge=8, le=512)
    height: int = Field(default=48, ge=8, le=512)


@router.post("/images", status_code=204)
def add_images(ctx: Ctx, body: ImageRequest) -> Response:
    """Put one real JPEG at every path, so previews and exact duplicates can be tested."""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (body.width, body.height), (40, 120, 200)).save(out, "JPEG")
    phone = _control(ctx).phone
    for path in body.paths:
        phone.add_file(path, out.getvalue(), mtime=1_700_000_000)
    return Response(status_code=204)
