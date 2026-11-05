"""Serve the built React UI (single-page app) from `web_dist`.

Implemented as a fallback for requests that match no route (an unmatched GET becomes the
static file or `index.html`) rather than as a catch-all route. That way it can never shadow
an API route, whatever order routes are registered in, and `/api/*` keeps its JSON 404.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

NOT_BUILT_HTML = (
    "<!doctype html><title>Sift</title><p>The Sift UI is not built. Run <code>make web</code>.</p>"
)


def _file_inside(root: Path, rel: str) -> Path | None:
    """The regular file `rel` under `root`, or None if it is missing or escapes `root`."""
    try:
        candidate = (root / rel.lstrip("/")).resolve()
    except OSError, ValueError:
        return None
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return None


def _static_response(root: Path | None, path: str) -> Response | None:
    if root is None:
        return HTMLResponse(NOT_BUILT_HTML) if path == "/" else None
    target = _file_inside(root, path) or _file_inside(root, "index.html")
    return FileResponse(target) if target is not None else None


def mount_web(app: FastAPI, web_dist: Path | None) -> None:
    """Serve the UI for unmatched non-API GETs. Call after `install_error_handlers`."""
    root = web_dist.resolve() if web_dist is not None else None
    json_handler = cast(
        Callable[[Request, Exception], Awaitable[Response]],
        app.exception_handlers[StarletteHTTPException],
    )

    async def handler(request: Request, exc: Exception) -> Response:
        path: str = request.scope["path"]
        is_api = path == "/api" or path.startswith("/api/")
        if (
            isinstance(exc, StarletteHTTPException)
            and exc.status_code == 404
            and request.method in ("GET", "HEAD")
            and not is_api
        ):
            response = _static_response(root, path)
            if response is not None:
                return response
        return await json_handler(request, exc)

    app.add_exception_handler(StarletteHTTPException, handler)
