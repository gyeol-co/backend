from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.errors import AppErrorCodeEnum, AppException
from app.core.redis import get_redis
from app.core.session_cookie import (
    SessionExpired,
    SessionInvalid,
    unsign_session,
)
from app.models import User

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]


async def require_auth(
    request: Request,
    db: DbSession,
) -> User:
    cookie_name = get_settings().SESSION_COOKIE_NAME
    token = request.cookies.get(cookie_name)
    if not token:
        raise AppException(AppErrorCodeEnum.AUTH_REQUIRED)
    try:
        user_id = unsign_session(token)
    except (SessionInvalid, SessionExpired) as e:
        raise AppException(AppErrorCodeEnum.AUTH_REQUIRED) from e

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise AppException(AppErrorCodeEnum.AUTH_REQUIRED)
    if not user.is_active:
        raise AppException(AppErrorCodeEnum.ACCOUNT_DEACTIVATED)
    return user


async def optional_auth(
    request: Request,
    db: DbSession,
) -> User | None:
    cookie_name = get_settings().SESSION_COOKIE_NAME
    token = request.cookies.get(cookie_name)
    if not token:
        return None
    try:
        user_id = unsign_session(token)
    except (SessionInvalid, SessionExpired):
        return None

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


def extract_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or None
    if request.client:
        return request.client.host
    return None


def extract_ua(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:1000] if ua else None


CurrentUser = Annotated[User, Depends(require_auth)]
OptionalUser = Annotated[User | None, Depends(optional_auth)]
IpAddr = Annotated[str | None, Depends(extract_ip)]
UserAgent = Annotated[str | None, Depends(extract_ua)]

__all__ = [
    "DbSession",
    "RedisDep",
    "CurrentUser",
    "OptionalUser",
    "IpAddr",
    "UserAgent",
]
