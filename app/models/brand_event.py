from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DeclarativeRoot, uuid7_std


class BrandEventType(StrEnum):
    VIEW = "view"
    CLICK = "click"
    FOLLOW = "follow"
    UNFOLLOW = "unfollow"


class BrandEvent(DeclarativeRoot):
    """Append-only brand interaction events (TimescaleDB hypertable).

    Schema has no real PK — hypertable partitions on `created`. ORM declares
    `id` as primary_key only to satisfy the mapper; inserts rely on the
    DB-side `uuidv7()` default (mirrored here via `insert_default=uuid7_std`).

    `user_id` is NOT NULL in schema — guest events cannot be inserted.
    """

    __tablename__ = "brand_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        init=False,
        primary_key=True,
        insert_default=uuid7_std,
    )
    # non-default (required-in-init) first
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[BrandEventType] = mapped_column(
        Enum(
            BrandEventType,
            name="brand_event_type",
            native_enum=True,
            create_type=False,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # default fields last
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default_factory=dict,
    )
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        nullable=False,
    )
