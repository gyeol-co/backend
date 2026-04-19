from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ProviderName = Literal["kakao", "naver", "google"]


@dataclass(frozen=True)
class ProviderTokens:
    access_token: str


@dataclass(frozen=True)
class ProviderUserInfo:
    provider_uid: str
    provider_email: str | None


class OAuthProviderError(Exception):
    """Raised when provider HTTP call fails, returns non-2xx, or lacks required fields."""


class OAuthProviderClient(Protocol):
    name: ProviderName

    def authorize_url(self, state: str, redirect_uri: str) -> str: ...

    async def exchange_code(self, code: str, redirect_uri: str) -> ProviderTokens: ...

    async def fetch_userinfo(self, access_token: str) -> ProviderUserInfo: ...


def get_provider(name: str) -> OAuthProviderClient:
    # Lazy imports to avoid circular dependency and keep import cost minimal.
    from app.oauth_providers.google import GoogleProvider
    from app.oauth_providers.kakao import KakaoProvider
    from app.oauth_providers.naver import NaverProvider

    if name == "kakao":
        return KakaoProvider()
    if name == "naver":
        return NaverProvider()
    if name == "google":
        return GoogleProvider()
    raise ValueError(f"unknown provider: {name}")
