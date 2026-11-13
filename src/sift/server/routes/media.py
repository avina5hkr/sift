"""Thumbnails, previews and the file itself for the Review screen, read from the backup copy.

Paths come only from the database (a phone_file id → its content's replica), never from the
request, so nothing outside a backup folder or the cache can be served. Backup files are only
read; thumbnails and previews are written to the cache folder.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from PIL import Image, ImageOps

from sift.analysis.index import THUMB_PX, thumb_path
from sift.server.deps import Ctx
from sift.server.errors import ApiError

router = APIRouter()

PREVIEW_MAX = 1600


def _source(ctx: Ctx, phone_file_id: int) -> tuple[str, Path, str | None]:
    """(content hash, a readable backup copy, mime) for a phone file."""
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT p.content_hash, d.root_path, r.rel_path, c.mime FROM phone_file p "
            "JOIN replica r ON r.content_hash = p.content_hash "
            "JOIN destination d ON d.id = r.dest_id JOIN content c ON c.hash = p.content_hash "
            "WHERE p.id = ? ORDER BY CASE d.kind WHEN 'mac' THEN 0 ELSE 1 END",
            (phone_file_id,),
        ).fetchall()
    for digest, root, rel, mime in rows:
        path = Path(root).expanduser() / rel
        if path.is_file():
            return str(digest), path, mime
    raise ApiError(404, "not_found", "There is no backed-up copy of this file to show.")


def _render(source: Path, target: Path, size: int) -> None:
    with Image.open(source) as raw:
        img = ImageOps.exif_transpose(raw)
        img.thumbnail((size, size))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=85)
        tmp.replace(target)


def _image(ctx: Ctx, phone_file_id: int, target_of: str, size: int) -> FileResponse:
    digest, path, _mime = _source(ctx, phone_file_id)
    target = (
        thumb_path(ctx.paths.cache, digest)
        if target_of == "thumb"
        else ctx.paths.cache / "previews" / digest[:2] / f"{digest}-{size}.jpg"
    )
    if not target.is_file():
        try:
            _render(path, target, size)
        except Exception as e:  # not an image, or a damaged one
            raise ApiError(415, "no_preview", "Sift cannot show a preview of this file.") from e
    return FileResponse(target, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@router.api_route("/media/{phone_file_id}/thumb", methods=["GET", "HEAD"])
def thumb(ctx: Ctx, phone_file_id: int) -> FileResponse:
    return _image(ctx, phone_file_id, "thumb", THUMB_PX)


@router.api_route("/media/{phone_file_id}/preview", methods=["GET", "HEAD"])
def preview(
    ctx: Ctx, phone_file_id: int, size: Annotated[int, Query(alias="max")] = PREVIEW_MAX
) -> FileResponse:
    return _image(ctx, phone_file_id, "preview", min(PREVIEW_MAX, max(64, size)))


@router.api_route("/media/{phone_file_id}/file", methods=["GET", "HEAD"])
def original(ctx: Ctx, phone_file_id: int) -> FileResponse:
    """The backed-up file itself, with HTTP Range support (videos seek with it)."""
    _digest, path, mime = _source(ctx, phone_file_id)
    return FileResponse(path, media_type=mime or "application/octet-stream")
