from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.oauth_providers.base import (
    OAuthProviderError,
    ProviderName,
    ProviderTokens,
    ProviderUserInfo,
)

_AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
_USERINFO_URL = "https://kapi.kakao.com/v2/user/me"
_SCOPE = "account_email profile_nickname"


class KakaoProvider:
    name: ProviderName = "kakao"

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "response_type": "code",
            "client_id": get_settings().KAKAO_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": _SCOPE,
            "state": state,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> ProviderTokens:
        settings = get_settings()
        data = {
            "grant_type": "authorization_code",
            "client_id": settings.KAKAO_CLIENT_ID,
            "client_secret": settings.KAKAO_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    _TOKEN_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as e:
            raise OAuthProviderError(f"kakao token request failed: {e}") from e
        if resp.status_code != 200:
            raise OAuthProviderError(f"kakao token status {resp.status_code}: {resp.text}")
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:
            raise OAuthProviderError("kakao token response missing access_token")
        return ProviderTokens(access_token=access_token)

    async def fetch_userinfo(self, access_token: str) -> ProviderUserInfo:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    _USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as e:
            raise OAuthProviderError(f"kakao userinfo request failed: {e}") from e
        if resp.status_code != 200:
            raise OAuthProviderError(f"kakao userinfo status {resp.status_code}: {resp.text}")
        body = resp.json()
        uid = body.get("id")
        if uid is None:
            raise OAuthProviderError("kakao userinfo missing id")
        account = body.get("kakao_account") or {}
        email = account.get("email")
        return ProviderUserInfo(provider_uid=str(uid), provider_email=email)
