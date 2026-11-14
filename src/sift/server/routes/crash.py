"""The crash dialog (spec 14): the newest unseen crash report, mark it seen, open the logs."""

import subprocess
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from sift.server.deps import Ctx
from sift.services import crash

router = APIRouter()


@router.get("/crash")
def get_crash(ctx: Ctx) -> dict[str, Any]:
    return {"crash": crash.latest_unseen(ctx)}


class Seen(BaseModel):
    report: str


@router.post("/crash/seen", status_code=204)
def seen(ctx: Ctx, body: Seen) -> None:
    crash.mark_seen(ctx, body.report)


def _open(path: str) -> None:
    subprocess.run(["/usr/bin/open", path], check=False, timeout=10)


@router.post("/logs/open", status_code=204)
def open_logs(ctx: Ctx) -> None:
    """Show the logs folder in Finder."""
    ctx.paths.logs.mkdir(parents=True, exist_ok=True)
    _open(str(ctx.paths.logs))
