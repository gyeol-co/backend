from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import CurrentUser, DbSession, IpAddr, RedisDep, UserAgent
from app.core.config import get_settings
from app.core.envelope import StandardResponseMeta, StandardSuccessResponse
from app.core.request_id import current_request_id
from app.core.session_cookie import sign_session
from app.schemas.auth import (
    EmailLoginRequest,
    EmailSignupRequest,
    LogoutResponse,
    OAuthCallbackResponse,
    OAuthSignupRequest,
    UserEnvelope,
    UserResponse,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------- helpers ----------


def _to_user_response(u: auth_service.UserWithDerived) -> UserResponse:
    return UserResponse(
        id=u.user.id,
        email=u.user.email or "",
        display_name=u.user.display_name,
        styles=list(u.user.styles or []),
        has_password=u.has_password,
        oauth_providers=u.oauth_providers,
        visited=u.user.visited,
        created=u.user.created,
    )


def _meta() -> StandardResponseMeta:
    return StandardResponseMeta(request_id=current_request_id())


def _set_session_cookie(response: Response, user_id: UUID) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=sign_session(user_id),
        max_age=settings.SESSION_MAX_AGE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )


def _set_oauth_state_cookie(response: Response, value: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.OAUTH_STATE_COOKIE_NAME,
        value=value,
        max_age=settings.OAUTH_STATE_MAX_AGE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN,
        path="/auth/oauth",
    )


def _clear_oauth_state_cookie(response: Response) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.OAUTH_STATE_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN,
        path="/auth/oauth",
    )


# ---------- 1. POST /auth/signup/email ----------


@router.post(
    "/signup/email",
    response_model=StandardSuccessResponse[UserEnvelope],
    name="auth.signup.email",
)
async def _(
    body: EmailSignupRequest,
    db: DbSession,
    response: Response,
    ip: IpAddr,
    ua: UserAgent,
) -> StandardSuccessResponse[UserEnvelope]:
    result = await auth_service.signup_email(
        db,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        consents=body.consents,
        ip=ip,
        ua=ua,
    )
    _set_session_cookie(response, result.user.id)
    return StandardSuccessResponse(
        data=UserEnvelope(user=_to_user_response(result)),
        meta=_meta(),
    )


# ---------- 2. POST /auth/login/email ----------


@router.post(
    "/login/email",
    response_model=StandardSuccessResponse[UserEnvelope],
    name="auth.login.email",
)
async def _(
    body: EmailLoginRequest,
    db: DbSession,
    response: Response,
) -> StandardSuccessResponse[UserEnvelope]:
    result = await auth_service.login_email(db, email=body.email, password=body.password)
    _set_session_cookie(response, result.user.id)
    return StandardSuccessResponse(
        data=UserEnvelope(user=_to_user_response(result)),
        meta=_meta(),
    )


# ---------- 3. GET /auth/oauth/{provider}/authorize ----------


@router.get(
    "/oauth/{provider}/authorize",
    name="auth.oauth.authorize",
)
async def _(
    redis: RedisDep,
    provider: str = Path(...),
    next: str | None = Query(default=None),
) -> RedirectResponse:
    url, state_value = await auth_service.get_authorize_url(redis, provider, next)
    redirect = RedirectResponse(url=url, status_code=302)
    _set_oauth_state_cookie(redirect, state_value)
    return redirect


# ---------- 4. GET /auth/oauth/{provider}/callback ----------


@router.get(
    "/oauth/{provider}/callback",
    response_model=StandardSuccessResponse[OAuthCallbackResponse],
    name="auth.oauth.callback",
)
async def _(
    request: Request,
    db: DbSession,
    redis: RedisDep,
    response: Response,
    provider: str = Path(...),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> StandardSuccessResponse[OAuthCallbackResponse]:
    settings = get_settings()
    state_cookie = request.cookies.get(settings.OAUTH_STATE_COOKIE_NAME)

    result = await auth_service.handle_oauth_callback(
        db,
        redis,
        provider_name=provider,
        code=code,
        state_query=state,
        state_cookie=state_cookie,
        error_param=error,
    )

    # Always clear the oauth_state cookie after processing, regardless of branch.
    _clear_oauth_state_cookie(response)

    if isinstance(result, auth_service.CallbackLoggedIn):
        _set_session_cookie(response, result.user.user.id)
        payload = OAuthCallbackResponse(
            status="logged_in",
            user=_to_user_response(result.user),
            next=result.next,
        )
    else:
        payload = OAuthCallbackResponse(
            status="needs_signup",
            pending_token=result.pending_token,
            provider=result.provider,
            provider_email=result.provider_email,
            next=result.next,
        )

    return StandardSuccessResponse(data=payload, meta=_meta())


# ---------- 5. POST /auth/signup/oauth ----------


@router.post(
    "/signup/oauth",
    response_model=StandardSuccessResponse[UserEnvelope],
    name="auth.signup.oauth",
)
async def _(
    body: OAuthSignupRequest,
    db: DbSession,
    redis: RedisDep,
    response: Response,
    ip: IpAddr,
    ua: UserAgent,
) -> StandardSuccessResponse[UserEnvelope]:
    result = await auth_service.signup_oauth(
        db,
        redis,
        pending_token=body.pending_token,
        display_name=body.display_name,
        consents=body.consents,
        ip=ip,
        ua=ua,
    )
    _set_session_cookie(response, result.user.id)
    return StandardSuccessResponse(
        data=UserEnvelope(user=_to_user_response(result)),
        meta=_meta(),
    )


# ---------- 6. POST /auth/logout ----------


@router.post(
    "/logout",
    response_model=StandardSuccessResponse[LogoutResponse],
    name="auth.logout",
)
async def _(response: Response) -> StandardSuccessResponse[LogoutResponse]:
    _clear_session_cookie(response)
    return StandardSuccessResponse(data=LogoutResponse(), meta=_meta())


# ---------- 7. GET /auth/me ----------


@router.get(
    "/me",
    response_model=StandardSuccessResponse[UserEnvelope],
    name="auth.me",
)
async def _(
    user: CurrentUser,
    db: DbSession,
) -> StandardSuccessResponse[UserEnvelope]:
    result = await auth_service.get_me(db, user.id)
    return StandardSuccessResponse(
        data=UserEnvelope(user=_to_user_response(result)),
        meta=_meta(),
    )
