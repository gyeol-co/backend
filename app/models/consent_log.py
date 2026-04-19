from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DeclarativeRoot, uuid7_std


class ConsentType(StrEnum):
    terms = "terms"
    privacy = "privacy"
    marketing = "marketing"
    age_14_plus = "age_14_plus"


class ConsentMethod(StrEnum):
    signup = "signup"
    settings = "settings"
    re_consent = "re_consent"


class ConsentLog(DeclarativeRoot):
    """Append-only: grant/revoke both INSERT."""

    __tablename__ = "consent_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        init=False,
        primary_key=True,
        insert_default=uuid7_std,
    )
    # non-default fields first (dataclass ordering)
    type: Mapped[ConsentType] = mapped_column(
        Enum(ConsentType, name="consent_type", native_enum=True, create_type=False),
        nullable=False,
    )
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    method: Mapped[ConsentMethod] = mapped_column(
        Enum(ConsentMethod, name="consent_method", native_enum=True, create_type=False),
        nullable=False,
    )
    # nullable / default fields last
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True, default=None)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        nullable=False,
    )
