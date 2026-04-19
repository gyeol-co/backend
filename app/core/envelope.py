from typing import Any

from pydantic import BaseModel, ConfigDict


class ErrorMeta(BaseModel):
    code: str
    detail_payload: dict[str, Any] | None = None


class StandardResponseMeta(BaseModel):
    request_id: str
    next: dict[str, Any] | None = None
    error: ErrorMeta | None = None


class StandardSuccessResponse[T](BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: T
    meta: StandardResponseMeta


class StandardErrorResponse(BaseModel):
    """Used by the global exception handler. `data` is always null on error."""

    data: None = None
    meta: StandardResponseMeta
