from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseWithUuidId


class Brand(BaseWithUuidId):
    __tablename__ = "brand"

    # non-default (required-in-init) first
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # default / nullable fields last
    name_ko: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    logo_key: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    cover_key: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    styles: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default_factory=list,
    )
    website_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    is_featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    product_added: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
