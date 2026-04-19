from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

ConsentType = Literal["terms", "privacy", "marketing", "age_14_plus"]
ConsentMethod = Literal["signup", "settings", "re_consent"]
OAuthProvider = Literal["kakao", "naver", "google"]


class ConsentEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ConsentType
    policy_version: str = Field(min_length=1, max_length=32)
    is_granted: bool
    method: ConsentMethod


class EmailSignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=40)
    consents: list[ConsentEntryInput] = Field(min_length=4, max_length=4)


class EmailLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class OAuthSignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending_token: str = Field(min_length=1, max_length=2048)
    display_name: str = Field(min_length=1, max_length=40)
    consents: list[ConsentEntryInput] = Field(min_length=4, max_length=4)


class UserResponse(BaseModel):
    """User public representation. `has_password` / `oauth_providers` computed by service."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str
    styles: list[str]
    has_password: bool
    oauth_providers: list[OAuthProvider]
    visited: datetime | None
    created: datetime


class UserEnvelope(BaseModel):
    user: UserResponse


class OAuthCallbackLoggedIn(BaseModel):
    status: Literal["logged_in"] = "logged_in"
    user: UserResponse
    next: str


class OAuthCallbackNeedsSignup(BaseModel):
    status: Literal["needs_signup"] = "needs_signup"
    pending_token: str
    provider: OAuthProvider
    provider_email: EmailStr
    next: str


class OAuthCallbackResponse(BaseModel):
    """Discriminated on `status` — one of `logged_in` or `needs_signup`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["logged_in", "needs_signup"]
    user: UserResponse | None = None
    pending_token: str | None = None
    provider: OAuthProvider | None = None
    provider_email: EmailStr | None = None
    next: str


class LogoutResponse(BaseModel):
    logged_out: Literal[True] = True
