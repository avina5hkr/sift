"""One error shape for the whole API: `{code, message, detail}`."""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sift.backup.lease import SsdBusy

logger = logging.getLogger("sift.server")

INTERNAL_MESSAGE = "Something went wrong. Details are in Logs."


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def error_response(status: int, code: str, message: str, detail: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"code": code, "message": message, "detail": detail}
    )


async def _api_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.code, exc.message, exc.detail)


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        422, "invalid_request", "The request was not valid.", jsonable_errors(exc.errors())
    )


def jsonable_errors(errors: Any) -> Any:
    """Drop the `ctx`/`input` echo from pydantic errors; keep type, location and message."""
    return [{k: v for k, v in e.items() if k in ("type", "loc", "msg")} for e in errors]


async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        return error_response(404, "not_found", "Not found.")
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "Error"
    code = phrase.lower().replace(" ", "_")
    return error_response(exc.status_code, code, f"{phrase}.")


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(500, "internal", INTERNAL_MESSAGE)


async def _ssd_busy(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, SsdBusy)
    return error_response(409, "ssd_busy", exc.message)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(SsdBusy, _ssd_busy)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
