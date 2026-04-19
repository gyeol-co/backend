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

_AUTHORIZE_URL = "https://nid.naver.com/oauth2.0/authorize"
_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
_USERINFO_URL = "https://openapi.naver.com/v1/nid/me"
_SCOPE = "name,email"


class NaverProvider:
    name: ProviderName = "naver"

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "response_type": "code",
            "client_id": get_settings().NAVER_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": _SCOPE,
            "state": state,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> ProviderTokens:
        settings = get_settings()
        params = {
            "grant_type": "authorization_code",
            "client_id": settings.NAVER_CLIENT_ID,
            "client_secret": settings.NAVER_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_TOKEN_URL, params=params)
        except httpx.HTTPError as e:
            raise OAuthProviderError(f"naver token request failed: {e}") from e
        if resp.status_code != 200:
            raise OAuthProviderError(f"naver token status {resp.status_code}: {resp.text}")
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:
            raise OAuthProviderError("naver token response missing access_token")
        return ProviderTokens(access_token=access_token)

    async def fetch_userinfo(self, access_token: str) -> ProviderUserInfo:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    _USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as e:
            raise OAuthProviderError(f"naver userinfo request failed: {e}") from e
        if resp.status_code != 200:
            raise OAuthProviderError(f"naver userinfo status {resp.status_code}: {resp.text}")
        body = resp.json()
        if body.get("resultcode") != "00":
            raise OAuthProviderError(f"naver userinfo resultcode {body.get('resultcode')}")
        response = body.get("response") or {}
        uid = response.get("id")
        if not uid:
            raise OAuthProviderError("naver userinfo missing id")
        email = response.get("email")
        return ProviderUserInfo(provider_uid=str(uid), provider_email=email)
