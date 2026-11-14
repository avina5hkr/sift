"""Logs screen and diagnostics export (spec 11). Live lines arrive as `log` events; this
reads the log files for the table, and writes a diagnostics zip for sharing."""

import contextlib
import json
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from sift.core.logging import LOG_FILE
from sift.server.deps import Ctx
from sift.server.errors import ApiError

router = APIRouter()

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_PHONE_PATH = re.compile(r"/(?:sdcard|storage/emulated/\d+|storage/[0-9A-F-]{9})/[^\s\"',;)]*")


def _log_files(ctx: Ctx) -> list[Path]:
    """Newest first: sift.log, sift.log.1, ..."""
    base = ctx.paths.logs / LOG_FILE
    files = [base] + [base.with_name(f"{LOG_FILE}.{n}") for n in range(1, 6)]
    return [f for f in files if f.exists()]


def _entries(ctx: Ctx, cap: int = 20_000) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in _log_files(ctx):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and "message" in e:
                out.append(e)
                if len(out) >= cap:
                    return out
    return out


@router.get("/logs")
def get_logs(
    ctx: Ctx,
    level: str = Query("INFO"),
    area: str = Query(""),
    q: str = Query("", max_length=200),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """The newest log lines at `level` or above, filtered by area and text."""
    floor = LEVELS.index(level.upper()) if level.upper() in LEVELS else 1
    needle = q.casefold()
    rows = []
    for e in _entries(ctx):
        lv = str(e.get("level", "INFO"))
        if (LEVELS.index(lv) if lv in LEVELS else 1) < floor:
            continue
        if area and not str(e.get("area", "")).startswith(area):
            continue
        if needle and needle not in str(e.get("message", "")).casefold():
            continue
        rows.append({k: e.get(k) for k in ("ts", "level", "area", "message")})
        if len(rows) >= limit:
            break
    return {"entries": rows}


def redact(text: str, ctx: Ctx) -> str:
    """File paths out: the backup folders, the home folder and phone paths get placeholders."""
    from sift.backup.run import usable_dests

    roots = [ctx.config.backup.mac_root, str(Path.home())]
    with contextlib.suppress(Exception):
        roots += [str(d.root) for d in usable_dests(ctx)]
    for root in sorted({str(Path(r).expanduser()) for r in roots if r}, key=len, reverse=True):
        text = text.replace(root, "<backup>" if root != str(Path.home()) else "~")
    return _PHONE_PATH.sub("<phone path>", text)


def _schema_version(ctx: Ctx) -> int | None:
    from sift.core.migrations import schema_version

    try:
        return schema_version(ctx.db)
    except sqlite3.Error:
        return None


def downloads_dir() -> Path:
    return Path.home() / "Downloads"


class DiagnosticsRequest(BaseModel):
    include_paths: bool = False


@router.post("/diagnostics")
def export_diagnostics(ctx: Ctx, body: DiagnosticsRequest) -> dict[str, str]:
    """A zip with the logs, the config and the schema version, in ~/Downloads. File paths are
    left in only when the owner ticks "include file paths"."""
    folder = downloads_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ApiError(409, "no_folder", f"Sift could not use {folder.name}: {e}") from e
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = folder / f"sift-diagnostics-{stamp}.zip"
    n = 1
    while path.exists():
        path = folder / f"sift-diagnostics-{stamp}-{n}.zip"
        n += 1

    def clean(text: str) -> str:
        return text if body.include_paths else redact(text, ctx)

    try:
        with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as z:
            for f in _log_files(ctx):
                z.writestr(f"logs/{f.name}", clean(f.read_text(encoding="utf-8", errors="replace")))
            if ctx.paths.config.exists():
                z.writestr("config.toml", clean(ctx.paths.config.read_text(encoding="utf-8")))
            z.writestr(
                "about.json",
                json.dumps(
                    {
                        "version": ctx.version,
                        "schema_version": _schema_version(ctx),
                        "paths_included": body.include_paths,
                        "config_warnings": [clean(w) for w in ctx.config_warnings],
                    },
                    indent=2,
                ),
            )
    except OSError as e:
        raise ApiError(409, "not_saved", f"The diagnostics could not be saved: {e}") from e
    return {"path": str(path), "name": path.name}
