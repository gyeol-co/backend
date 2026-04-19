from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DeclarativeRoot, uuid7_std


class SearchEvent(DeclarativeRoot):
    """Append-only search query events (TimescaleDB hypertable).

    Schema has no real PK — hypertable partitions on `created`. ORM declares
    `id` as primary_key only to satisfy the mapper; inserts rely on the
    DB-side `uuidv7()` default (mirrored here via `insert_default=uuid7_std`).

    `user_id` is NOT NULL in schema — guest events cannot be inserted. No
    enum type (plain text query); no `properties` jsonb (append-only audit).
    """

    __tablename__ = "search_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        init=False,
        primary_key=True,
        insert_default=uuid7_std,
    )
    # non-default (required-in-init) first
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # default fields last
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        nullable=False,
    )
