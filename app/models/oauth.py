from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DeclarativeRoot, uuid7_std


class OAuth(DeclarativeRoot):
    """oauth has only `created` (no `updated`) — can't inherit Base."""

    __tablename__ = "oauth"
    __table_args__ = (Index("uq_oauth_provider_uid", "provider", "provider_uid", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        init=False,
        primary_key=True,
        insert_default=uuid7_std,
    )
    # non-default fields first
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_uid: Mapped[str] = mapped_column(Text, nullable=False)
    # default fields last
    provider_email: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        nullable=False,
    )
