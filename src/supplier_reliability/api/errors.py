"""Error envelope {"error": {"code", "message", "details"}} and the handlers that produce it.

Codes: not_found 404, conflict 409, validation_error 422, payload_too_large 413,
internal_error 500 (generic message; the exception is logged server-side only)."""

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from supplier_reliability.schemas.common import ErrorEnvelope, details_of

logger = logging.getLogger(__name__)

_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
}


MAX_LISTED_IDS = 10


def bounded_list(items: Sequence[str], limit: int = MAX_LISTED_IDS) -> str:
    """At most `limit` items comma-joined, then "(and N more)": keeps messages bounded."""
    shown = ", ".join(items[:limit])
    extra = len(items) - limit
    return shown if extra <= 0 else f"{shown} (and {extra} more)"


class NotFoundError(Exception):
    def __init__(self, resource: str, identifier: str) -> None:
        self.resource, self.identifier = resource, identifier
        super().__init__(f"{resource} {identifier!r} not found")


class ConflictError(Exception):
    """A documented state conflict (HTTP 409)."""


class InvalidRequestError(Exception):
    """Semantically invalid request data (HTTP 422) that Pydantic alone cannot express, e.g. a
    sort field outside the endpoint whitelist. `msg` must not contain raw user input."""

    def __init__(self, loc: list[str | int], msg: str, type_: str = "value_error") -> None:
        self.loc, self.msg, self.type = loc, msg, type_
        super().__init__(msg)


def envelope(code: str, message: str, details: list[dict[str, Any]] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def error_response(
    status: int, code: str, message: str, details: list[dict[str, Any]] | None = None
) -> JSONResponse:
    return JSONResponse(status_code=status, content=envelope(code, message, details))


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries documenting the envelope for each status in OpenAPI."""
    text = {
        404: "Resource not found",
        409: "State conflict",
        413: "Request body too large",
        422: "Invalid request data",
        503: "Service not ready",
    }
    return {s: {"model": ErrorEnvelope, "description": text[s]} for s in statuses}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return error_response(404, "not_found", f"{exc.resource} '{exc.identifier}' not found")

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return error_response(409, "conflict", str(exc))

    @app.exception_handler(InvalidRequestError)
    async def _invalid(_: Request, exc: InvalidRequestError) -> JSONResponse:
        details = [{"loc": exc.loc, "msg": exc.msg, "type": exc.type}]
        return error_response(422, "validation_error", "Request validation failed", details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422, "validation_error", "Request validation failed", details_of(exc.errors())
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return error_response(500, "internal_error", "Internal server error")
