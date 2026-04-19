from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppErrorCodeEnum, AppException
from app.core.oauth_tokens import (
    OAuthStateInvalid,
    PendingTokenInvalid,
    consume_oauth_state,
    consume_pending_token,
    issue_oauth_state,
    issue_pending_token,
)
from app.core.password import dummy_verify, hash_password, verify_password
from app.models import ConsentLog, ConsentMethod, ConsentType, OAuth, User
from app.oauth_providers import OAuthProviderError, get_provider
from app.schemas.auth import ConsentEntryInput, OAuthProvider

logger = logging.getLogger("gyeol.auth")

_REQUIRED_CONSENTS: set[str] = {"terms", "privacy", "age_14_plus"}
_ALL_CONSENTS: set[str] = {"terms", "privacy", "age_14_plus", "marketing"}


# -------- internal helpers --------


@dataclass(frozen=True)
class UserWithDerived:
    user: User
    oauth_providers: list[OAuthProvider]

    @property
    def has_password(self) -> bool:
        return self.user.password_hash is not None


def _validate_consents(consents: list[ConsentEntryInput]) -> None:
    types = [c.type for c in consents]
    if set(types) != _ALL_CONSENTS:
        raise AppException(AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "consents"})
    if len(types) != len(set(types)):
        raise AppException(AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "consents"})
    for c in consents:
        if c.type in _REQUIRED_CONSENTS and not c.is_granted:
            raise AppException(AppErrorCodeEnum.CONSENT_REQUIRED, detail_payload={"type": c.type})


def _providers_expr():
    """array_agg(oauth.provider ORDER BY oauth.created) FILTER (WHERE oauth.id IS NOT NULL)."""
    return (
        func.array_agg(
            OAuth.provider,
        )
        .filter(OAuth.id.is_not(None))
        .label("providers")
    )


async def _fetch_user_with_derived_by_id(db: AsyncSession, user_id: UUID) -> UserWithDerived | None:
    stmt = (
        select(User, _providers_expr())
        .outerjoin(OAuth, OAuth.user_id == User.id)
        .where(User.id == user_id)
        .group_by(User.id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    user_obj, providers = row
    return UserWithDerived(
        user=user_obj,
        oauth_providers=cast(list[OAuthProvider], list(providers or [])),
    )


async def _fetch_user_with_derived_by_email(
    db: AsyncSession, email_lower: str
) -> UserWithDerived | None:
    stmt = (
        select(User, _providers_expr())
        .outerjoin(OAuth, OAuth.user_id == User.id)
        .where(func.lower(User.email) == email_lower)
        .group_by(User.id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    user_obj, providers = row
    return UserWithDerived(
        user=user_obj,
        oauth_providers=cast(list[OAuthProvider], list(providers or [])),
    )


def _insert_consents(
    db: AsyncSession,
    user_id: UUID,
    consents: list[ConsentEntryInput],
    ip: str | None,
    ua: str | None,
) -> None:
    for c in consents:
        db.add(
            ConsentLog(
                user_id=user_id,
                type=ConsentType(c.type),
                policy_version=c.policy_version,
                is_granted=c.is_granted,
                method=ConsentMethod.signup,
                ip_address=ip,
                user_agent=ua,
            )
        )


# -------- public API --------


async def signup_email(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    consents: list[ConsentEntryInput],
    ip: str | None,
    ua: str | None,
) -> UserWithDerived:
    _validate_consents(consents)
    email_lower = email.lower()
    display_name_norm = display_name.strip()
    if not display_name_norm:
        raise AppException(
            AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "display_name"}
        )

    try:
        existing = await db.execute(
            select(User.id).where(func.lower(User.email) == email_lower)
        )
        if existing.scalar_one_or_none() is not None:
            raise AppException(AppErrorCodeEnum.EMAIL_ALREADY_EXISTS)

        user = User(
            email=email_lower,
            password_hash=hash_password(password),
            password_changed=None,
            display_name=display_name_norm,
            styles=[],
            is_active=True,
            visited=None,
        )
        # password_changed server default — use SQL now() via flush + UPDATE pattern.
        db.add(user)
        await db.flush()
        # set password_changed = now() in the same transaction
        await db.execute(
            update(User).where(User.id == user.id).values(password_changed=func.now())
        )
        _insert_consents(db, user.id, consents, ip, ua)
    except IntegrityError as e:
        # concurrent signup race → unique violation on email
        logger.info("auth.signup.email.race email=%s", email_lower)
        raise AppException(AppErrorCodeEnum.EMAIL_ALREADY_EXISTS) from e

    # refetch with derived fields (password_changed, etc.)
    result = await _fetch_user_with_derived_by_id(db, user.id)
    assert result is not None
    logger.info("auth.signup.email.ok user_id=%s", result.user.id)
    return result


async def login_email(
    db: AsyncSession,
    *,
    email: str,
    password: str,
) -> UserWithDerived:
    email_lower = email.lower()
    found = await _fetch_user_with_derived_by_email(db, email_lower)

    if found is None:
        dummy_verify()
        logger.info("auth.login.email.fail reason=not_found")
        raise AppException(AppErrorCodeEnum.INVALID_CREDENTIALS)

    if found.user.password_hash is None:
        dummy_verify()
        logger.info("auth.login.email.fail reason=oauth_only user_id=%s", found.user.id)
        raise AppException(AppErrorCodeEnum.INVALID_CREDENTIALS)

    if not verify_password(found.user.password_hash, password):
        logger.info("auth.login.email.fail reason=bad_password user_id=%s", found.user.id)
        raise AppException(AppErrorCodeEnum.INVALID_CREDENTIALS)

    if not found.user.is_active:
        logger.info("auth.login.email.fail reason=deactivated user_id=%s", found.user.id)
        raise AppException(AppErrorCodeEnum.ACCOUNT_DEACTIVATED)

    await db.execute(
        update(User).where(User.id == found.user.id).values(visited=func.now(), updated=func.now())
    )
    await db.commit()

    refreshed = await _fetch_user_with_derived_by_id(db, found.user.id)
    assert refreshed is not None
    logger.info("auth.login.email.ok user_id=%s", refreshed.user.id)
    return refreshed


# -------- OAuth --------


def validate_next_path(next_path: str | None) -> str:
    if not next_path:
        return "/discover"
    # only allow relative root-anchored paths
    if next_path.startswith("//") or "://" in next_path or not next_path.startswith("/"):
        raise AppException(AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "next"})
    return next_path


async def get_authorize_url(
    redis: Redis, provider_name: str, next_path: str | None
) -> tuple[str, str]:
    """Return (provider_authorize_url, oauth_state_cookie_value).

    state nonce is stored in Redis with TTL; cookie & query both carry the same
    nonce for double-submit CSRF. `consume_oauth_state` GETDELs on callback.
    """
    if provider_name not in ("kakao", "naver", "google"):
        raise AppException(AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "provider"})
    next_p = validate_next_path(next_path)

    settings = get_settings()
    redirect_uri = _redirect_uri_for(provider_name, settings)
    provider = get_provider(provider_name)

    state_value = await issue_oauth_state(redis, provider=provider_name, next_path=next_p)
    url = provider.authorize_url(state=state_value, redirect_uri=redirect_uri)
    return url, state_value


def _redirect_uri_for(provider_name: str, settings: Settings) -> str:
    if provider_name == "kakao":
        return settings.KAKAO_REDIRECT_URI
    if provider_name == "naver":
        return settings.NAVER_REDIRECT_URI
    return settings.GOOGLE_REDIRECT_URI


@dataclass(frozen=True)
class CallbackLoggedIn:
    kind: Literal["logged_in"]
    user: UserWithDerived
    next: str


@dataclass(frozen=True)
class CallbackNeedsSignup:
    kind: Literal["needs_signup"]
    pending_token: str
    provider: OAuthProvider
    provider_email: str
    next: str


CallbackResult = CallbackLoggedIn | CallbackNeedsSignup


async def handle_oauth_callback(
    db: AsyncSession,
    redis: Redis,
    *,
    provider_name: str,
    code: str | None,
    state_query: str | None,
    state_cookie: str | None,
    error_param: str | None,
) -> CallbackResult:
    if provider_name not in ("kakao", "naver", "google"):
        raise AppException(AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "provider"})

    if error_param:
        logger.info("auth.oauth.callback.fail reason=user_denied provider=%s", provider_name)
        raise AppException(AppErrorCodeEnum.OAUTH_USER_DENIED)

    if not code:
        raise AppException(AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "code"})

    if not state_query or not state_cookie or state_query != state_cookie:
        raise AppException(AppErrorCodeEnum.OAUTH_STATE_MISMATCH)

    try:
        # Atomic GETDEL — single-use state. Raises if missing/expired/mismatch.
        state_payload = await consume_oauth_state(
            redis, nonce=state_query, expected_provider=provider_name
        )
    except OAuthStateInvalid as e:
        raise AppException(AppErrorCodeEnum.OAUTH_STATE_MISMATCH) from e

    next_p = state_payload.get("next") or "/discover"

    settings = get_settings()
    redirect_uri = _redirect_uri_for(provider_name, settings)
    provider = get_provider(provider_name)

    try:
        tokens = await provider.exchange_code(code=code, redirect_uri=redirect_uri)
        info = await provider.fetch_userinfo(access_token=tokens.access_token)
    except OAuthProviderError as e:
        logger.warning(
            "auth.oauth.callback.fail reason=provider provider=%s err=%s", provider_name, e
        )
        raise AppException(AppErrorCodeEnum.OAUTH_PROVIDER_ERROR) from e

    if not info.provider_email:
        logger.warning("auth.oauth.callback.fail reason=no_email provider=%s", provider_name)
        raise AppException(AppErrorCodeEnum.OAUTH_PROVIDER_ERROR)

    # Look for an existing oauth row (provider + provider_uid unique).
    existing_oauth_user_id = (
        await db.execute(
            select(OAuth.user_id).where(
                OAuth.provider == provider_name,
                OAuth.provider_uid == info.provider_uid,
            )
        )
    ).scalar_one_or_none()

    if existing_oauth_user_id is not None:
        found = await _fetch_user_with_derived_by_id(db, existing_oauth_user_id)
        if found is None:
            # Orphan oauth row — treat as no match, fall through to signup flow.
            existing_oauth_user_id = None
        else:
            if not found.user.is_active:
                logger.info("auth.oauth.callback.fail reason=deactivated user_id=%s", found.user.id)
                raise AppException(AppErrorCodeEnum.ACCOUNT_DEACTIVATED)
            await db.execute(
                update(User)
                .where(User.id == found.user.id)
                .values(visited=func.now(), updated=func.now())
            )
            await db.commit()
            refreshed = await _fetch_user_with_derived_by_id(db, found.user.id)
            assert refreshed is not None
            logger.info("auth.oauth.callback.logged_in user_id=%s", refreshed.user.id)
            return CallbackLoggedIn(kind="logged_in", user=refreshed, next=next_p)

    # Needs signup — issue pending token (Redis-backed, single-use).
    pending = await issue_pending_token(
        redis,
        provider=provider_name,
        provider_uid=info.provider_uid,
        provider_email=info.provider_email,
    )
    logger.info(
        "auth.oauth.callback.needs_signup provider=%s email=%s", provider_name, info.provider_email
    )
    return CallbackNeedsSignup(
        kind="needs_signup",
        pending_token=pending,
        provider=cast(OAuthProvider, provider_name),
        provider_email=info.provider_email,
        next=next_p,
    )


async def signup_oauth(
    db: AsyncSession,
    redis: Redis,
    *,
    pending_token: str,
    display_name: str,
    consents: list[ConsentEntryInput],
    ip: str | None,
    ua: str | None,
) -> UserWithDerived:
    try:
        # Atomic GETDEL — single-use. Missing / expired / already consumed all collapse here.
        payload = await consume_pending_token(redis, token=pending_token)
    except PendingTokenInvalid as e:
        raise AppException(AppErrorCodeEnum.PENDING_TOKEN_INVALID) from e

    provider_name = payload.get("provider")
    provider_uid = payload.get("provider_uid")
    provider_email = payload.get("provider_email")
    if not provider_name or not provider_uid or not provider_email:
        raise AppException(AppErrorCodeEnum.PENDING_TOKEN_INVALID)

    _validate_consents(consents)

    display_name_norm = display_name.strip()
    if not display_name_norm:
        raise AppException(
            AppErrorCodeEnum.VALIDATION_ERROR, detail_payload={"field": "display_name"}
        )

    email_lower = provider_email.lower()

    try:
        oauth_exists = (
            await db.execute(
                select(OAuth.id).where(
                    OAuth.provider == provider_name,
                    OAuth.provider_uid == provider_uid,
                )
            )
        ).scalar_one_or_none()
        if oauth_exists is not None:
            raise AppException(AppErrorCodeEnum.OAUTH_ALREADY_LINKED)

        user_exists = (
            await db.execute(select(User.id).where(func.lower(User.email) == email_lower))
        ).scalar_one_or_none()
        if user_exists is not None:
            raise AppException(AppErrorCodeEnum.EMAIL_ALREADY_EXISTS)

        user = User(
            email=email_lower,
            password_hash=None,
            password_changed=None,
            display_name=display_name_norm,
            styles=[],
            is_active=True,
            visited=None,
        )
        db.add(user)
        await db.flush()

        db.add(
            OAuth(
                user_id=user.id,
                provider=provider_name,
                provider_uid=provider_uid,
                provider_email=provider_email,
            )
        )
        _insert_consents(db, user.id, consents, ip, ua)
    except IntegrityError as e:
        # oauth(provider, provider_uid) UNIQUE violation or email UNIQUE violation
        msg = str(e.orig).lower() if e.orig else ""
        if "oauth" in msg or "provider_uid" in msg:
            raise AppException(AppErrorCodeEnum.OAUTH_ALREADY_LINKED) from e
        raise AppException(AppErrorCodeEnum.EMAIL_ALREADY_EXISTS) from e

    result = await _fetch_user_with_derived_by_id(db, user.id)
    assert result is not None
    logger.info("auth.signup.oauth.ok user_id=%s provider=%s", result.user.id, provider_name)
    return result


# -------- me --------


async def get_me(db: AsyncSession, user_id: UUID) -> UserWithDerived:
    found = await _fetch_user_with_derived_by_id(db, user_id)
    if found is None:
        raise AppException(AppErrorCodeEnum.AUTH_REQUIRED)
    if not found.user.is_active:
        raise AppException(AppErrorCodeEnum.ACCOUNT_DEACTIVATED)
    return found
