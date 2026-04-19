from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ErrorCodeProtocol(Protocol):
    """Common interface for error code enums across features."""

    value: str

    @property
    def http_status(self) -> int: ...


class AppErrorCodeEnum(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ACCOUNT_DEACTIVATED = "ACCOUNT_DEACTIVATED"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    EMAIL_ALREADY_EXISTS = "EMAIL_ALREADY_EXISTS"
    OAUTH_ALREADY_LINKED = "OAUTH_ALREADY_LINKED"
    OAUTH_STATE_MISMATCH = "OAUTH_STATE_MISMATCH"
    OAUTH_USER_DENIED = "OAUTH_USER_DENIED"
    OAUTH_PROVIDER_ERROR = "OAUTH_PROVIDER_ERROR"
    PENDING_TOKEN_INVALID = "PENDING_TOKEN_INVALID"
    PENDING_TOKEN_EXPIRED = "PENDING_TOKEN_EXPIRED"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    WISHLIST_FULL = "WISHLIST_FULL"
    BRAND_NOT_FOUND = "BRAND_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self]


_HTTP_STATUS: dict[AppErrorCodeEnum, int] = {
    AppErrorCodeEnum.VALIDATION_ERROR: 422,
    AppErrorCodeEnum.AUTH_REQUIRED: 401,
    AppErrorCodeEnum.INVALID_CREDENTIALS: 401,
    AppErrorCodeEnum.ACCOUNT_DEACTIVATED: 403,
    AppErrorCodeEnum.CONSENT_REQUIRED: 400,
    AppErrorCodeEnum.EMAIL_ALREADY_EXISTS: 409,
    AppErrorCodeEnum.OAUTH_ALREADY_LINKED: 409,
    AppErrorCodeEnum.OAUTH_STATE_MISMATCH: 400,
    AppErrorCodeEnum.OAUTH_USER_DENIED: 400,
    AppErrorCodeEnum.OAUTH_PROVIDER_ERROR: 502,
    AppErrorCodeEnum.PENDING_TOKEN_INVALID: 400,
    AppErrorCodeEnum.PENDING_TOKEN_EXPIRED: 401,
    AppErrorCodeEnum.PRODUCT_NOT_FOUND: 404,
    AppErrorCodeEnum.WISHLIST_FULL: 409,
    AppErrorCodeEnum.BRAND_NOT_FOUND: 404,
    AppErrorCodeEnum.INTERNAL_ERROR: 500,
}


class AppException(Exception):
    """Raised from routes/services; global handler converts to the standard envelope."""

    def __init__(
        self,
        error_code: AppErrorCodeEnum | ErrorCodeProtocol,
        *,
        detail_payload: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.detail_payload = detail_payload
        super().__init__(error_code.value)
