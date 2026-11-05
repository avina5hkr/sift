"""Host and token checks for every request, as a pure ASGI middleware.

Not `BaseHTTPMiddleware`: that wraps and buffers responses, which breaks streaming (SSE).
"""

import hmac
import logging
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sift.core.context import AppContext
from sift.server.errors import error_response

TOKEN_HEADER = b"x-sift-token"

_log = logging.getLogger("sift.server")


def _query_token_allowed(method: str, path: str) -> bool:
    """`?token=` exists for EventSource and <img>, which cannot set headers."""
    return method == "GET" and (path == "/api/events" or path.startswith("/api/media/"))


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope["headers"]:
        if key == name:
            return bytes(value)
    return None


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, ctx: AppContext) -> None:
        self.app = app
        self._allowed_hosts = ctx.allowed_hosts
        self._token = ctx.token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Method and path only: the query string and headers can carry the session token.
        _log.debug("%s %s", scope["method"], scope["path"])
        refusal = self._check(scope)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _check(self, scope: Scope) -> JSONResponse | None:
        host = _header(scope, b"host")
        if host is None or host.decode("latin-1") not in self._allowed_hosts:
            return error_response(403, "bad_host", "This request was refused.")
        path: str = scope["path"]
        if path != "/api" and not path.startswith("/api/"):
            return None
        supplied = _header(scope, TOKEN_HEADER)
        if supplied is None and _query_token_allowed(scope["method"], path):
            values = parse_qs(scope["query_string"].decode("latin-1")).get("token")
            supplied = values[0].encode("utf-8") if values else None
        if supplied is None or not hmac.compare_digest(supplied, self._token):
            return error_response(401, "bad_token", "This request is not from the Sift window.")
        return None
