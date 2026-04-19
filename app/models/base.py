from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column
from uuid_utils import uuid7


def uuid7_std() -> uuid.UUID:
    """Return a stdlib uuid.UUID from uuid7 bytes (asyncpg expects stdlib UUID)."""
    return uuid.UUID(bytes=uuid7().bytes)


class DeclarativeRoot(MappedAsDataclass, DeclarativeBase):
    """Shared declarative root — no columns, for tables that don't fit standard shape."""


class Base(DeclarativeRoot):
    """Base with `created` + `updated` timestamp columns."""

    __abstract__ = True

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        nullable=False,
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BaseWithUuidId(Base):
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        init=False,
        primary_key=True,
        insert_default=uuid7_std,
    )
