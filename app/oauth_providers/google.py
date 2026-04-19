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

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_SCOPE = "openid email profile"


class GoogleProvider:
    name: ProviderName = "google"

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "response_type": "code",
            "client_id": get_settings().GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": _SCOPE,
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> ProviderTokens:
        settings = get_settings()
        data = {
            "grant_type": "authorization_code",
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
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
            raise OAuthProviderError(f"google token request failed: {e}") from e
        if resp.status_code != 200:
            raise OAuthProviderError(f"google token status {resp.status_code}: {resp.text}")
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:
            raise OAuthProviderError("google token response missing access_token")
        return ProviderTokens(access_token=access_token)

    async def fetch_userinfo(self, access_token: str) -> ProviderUserInfo:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    _USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as e:
            raise OAuthProviderError(f"google userinfo request failed: {e}") from e
        if resp.status_code != 200:
            raise OAuthProviderError(f"google userinfo status {resp.status_code}: {resp.text}")
        body = resp.json()
        uid = body.get("sub")
        if not uid:
            raise OAuthProviderError("google userinfo missing sub")
        email = body.get("email")
        return ProviderUserInfo(provider_uid=str(uid), provider_email=email)
