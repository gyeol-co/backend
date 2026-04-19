from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseWithUuidId


class User(BaseWithUuidId):
    __tablename__ = "user"

    # non-default (required-in-init) first
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # default fields last
    email: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True, default=None)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    password_changed: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    styles: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default_factory=list,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    visited: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
