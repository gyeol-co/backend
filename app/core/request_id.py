from __future__ import annotations

import contextvars

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from uuid_utils import uuid7

_REQUEST_ID_HEADER = "X-Request-ID"
_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def current_request_id() -> str:
    rid = _request_id_ctx.get()
    return rid if rid is not None else _new_request_id()


def _new_request_id() -> str:
    return "req_" + uuid7().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get(_REQUEST_ID_HEADER) or _new_request_id()
        token = _request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)
        response.headers[_REQUEST_ID_HEADER] = rid
        return response
