from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

_SESSION_SALT = "gyeol.session"


class SessionInvalid(Exception):
    pass


class SessionExpired(Exception):
    pass


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().SESSION_SECRET, salt=_SESSION_SALT)


def sign_session(user_id: UUID) -> str:
    payload = {"user_id": str(user_id), "issued_at": int(time.time())}
    return _serializer().dumps(payload)


def unsign_session(token: str) -> UUID:
    settings = get_settings()
    try:
        payload: dict[str, Any] = _serializer().loads(token, max_age=settings.SESSION_MAX_AGE)
    except SignatureExpired as e:
        raise SessionExpired() from e
    except BadSignature as e:
        raise SessionInvalid() from e
    try:
        return UUID(payload["user_id"])
    except (KeyError, ValueError, TypeError) as e:
        raise SessionInvalid() from e
