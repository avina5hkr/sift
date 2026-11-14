"""Browse and delete and the Archive browser (spec 10B, 10A, 12)."""

import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sift.cleanup import archive
from sift.cleanup.browse import Filters, level, load_rows, smart_lists, sort_key
from sift.cleanup.errors import RestoreRefused
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.cleanup import _device

router = APIRouter()
FILES_PAGE = 500


def _file(r: Any) -> dict[str, Any]:
    return {
        "id": r.id, "name": r.path.rsplit("/", 1)[-1], "path": r.path, "size": r.size,
        "mtime": r.mtime, "kind": r.kind, "origin": r.origin, "status": r.status,
    }  # fmt: skip


@router.get("/browse")
def browse(
    ctx: Ctx,
    path: str = "/sdcard",
    sort: str = "size",
    q: str = "",
    kind: str = "",
    older_days: int = 0,
    min_bytes: int = 0,
    origin: str = "",
    status: str = "",
    offset: int = 0,
) -> dict[str, Any]:
    """One folder level of the phone with sizes and backup status (from the last scan)."""
    if not path.startswith("/sdcard") or ".." in path.split("/"):
        raise ApiError(422, "bad_path", "Browse starts at /sdcard.")
    rows = load_rows(ctx.db, ctx.config, _device(ctx))
    folders, files = level(rows, path, Filters(q, kind, older_days, min_bytes, origin, status))
    folders.sort(key=sort_key(sort))
    files.sort(key=sort_key(sort))
    parts = path.rstrip("/").split("/")[1:]
    return {
        "path": path.rstrip("/") or "/sdcard",
        "crumbs": ["/" + "/".join(parts[: i + 1]) for i in range(len(parts))],
        "folders": [
            {
                "name": f.name,
                "path": f.path,
                "size": f.size,
                "files": f.files,
                "mtime": f.mtime,
                "status": f.status,
            }
            for f in folders
        ],
        "files": [_file(r) for r in files[offset : offset + FILES_PAGE]],
        "total_files": len(files),
        "total_size": sum(f.size for f in folders) + sum(r.size for r in files),
    }


@router.get("/browse/lists")
def lists(ctx: Ctx) -> list[dict[str, Any]]:
    rows = load_rows(ctx.db, ctx.config, _device(ctx))
    now = time.time()
    out = []
    for key, (title, pick) in smart_lists(ctx.config).items():
        hit = [r for r in rows if pick(r, now)]
        out.append(
            {"key": key, "title": title, "files": len(hit), "bytes": sum(r.size for r in hit)}
        )
    return out


@router.get("/browse/lists/{key}")
def list_files(ctx: Ctx, key: str, offset: int = 0) -> dict[str, Any]:
    found = smart_lists(ctx.config).get(key)
    if found is None:
        raise ApiError(404, "unknown_list", "There is no such list.")
    title, pick = found
    now = time.time()
    rows = sorted(
        (r for r in load_rows(ctx.db, ctx.config, _device(ctx)) if pick(r, now)),
        key=lambda r: -r.size,
    )
    return {
        "key": key,
        "title": title,
        "total_files": len(rows),
        "files": [_file(r) for r in rows[offset : offset + FILES_PAGE]],
    }


@router.get("/archive")
def archive_list(ctx: Ctx, dest: int, path: str = "") -> dict[str, Any]:
    try:
        folders, files = archive.listing(ctx, dest, path)
    except RestoreRefused as e:
        raise ApiError(409, "no_drive", str(e)) from e
    return {
        "dest": dest,
        "path": path.strip("/"),
        "folders": [f.__dict__ for f in folders],
        "files": [f.__dict__ for f in files],
    }


@router.get("/archive/destinations")
def archive_destinations(ctx: Ctx) -> list[dict[str, Any]]:
    from sift.backup.run import usable_dests

    return [{"id": d.id, "kind": d.kind, "root": d.root.as_posix()} for d in usable_dests(ctx)]


class ArchiveRestore(BaseModel):
    dest: int
    paths: list[str] = Field(min_length=1, max_length=5000)


@router.post("/archive/restore")
def archive_restore(ctx: Ctx, body: ArchiveRestore) -> dict[str, int]:
    session = ctx.tracker.session() if ctx.tracker is not None else None
    try:
        return archive.restore(ctx, body.dest, body.paths, session)
    except RestoreRefused as e:
        raise ApiError(409, "restore_refused", str(e)) from e
