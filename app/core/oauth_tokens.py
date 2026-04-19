from __future__ import annotations

import json
import secrets
from typing import TypedDict

from redis.asyncio import Redis

_STATE_PREFIX = "gyeol:oauth:state:"
_PENDING_PREFIX = "gyeol:oauth:pending:"
_TTL_SECONDS = 600


class OAuthStateInvalid(Exception):
    """state cookie/query missing, expired, already consumed, or provider mismatch."""


class PendingTokenInvalid(Exception):
    """pending_token missing, expired, or already consumed."""


class OAuthStatePayload(TypedDict):
    provider: str
    next: str


class PendingTokenPayload(TypedDict):
    provider: str
    provider_uid: str
    provider_email: str


# ---------- oauth state ----------


async def issue_oauth_state(
    redis: Redis,
    *,
    provider: str,
    next_path: str,
) -> str:
    nonce = secrets.token_urlsafe(32)
    payload: OAuthStatePayload = {"provider": provider, "next": next_path}
    await redis.set(_STATE_PREFIX + nonce, json.dumps(payload), ex=_TTL_SECONDS)
    return nonce


async def consume_oauth_state(
    redis: Redis,
    *,
    nonce: str,
    expected_provider: str,
) -> OAuthStatePayload:
    """Atomic GETDEL. Raises OAuthStateInvalid on miss / mismatch."""
    raw = await redis.getdel(_STATE_PREFIX + nonce)
    if raw is None:
        raise OAuthStateInvalid("state not found or expired")
    payload: OAuthStatePayload = json.loads(raw)
    if payload.get("provider") != expected_provider:
        raise OAuthStateInvalid("provider mismatch")
    return payload


# ---------- pending token (oauth signup) ----------


async def issue_pending_token(
    redis: Redis,
    *,
    provider: str,
    provider_uid: str,
    provider_email: str,
) -> str:
    token = secrets.token_urlsafe(32)
    payload: PendingTokenPayload = {
        "provider": provider,
        "provider_uid": provider_uid,
        "provider_email": provider_email,
    }
    await redis.set(_PENDING_PREFIX + token, json.dumps(payload), ex=_TTL_SECONDS)
    return token


async def consume_pending_token(
    redis: Redis,
    *,
    token: str,
) -> PendingTokenPayload:
    raw = await redis.getdel(_PENDING_PREFIX + token)
    if raw is None:
        raise PendingTokenInvalid("pending_token not found or expired")
    return json.loads(raw)
