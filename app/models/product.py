from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseWithUuidId


class ProductCategory(StrEnum):
    OUTER = "outer"
    TOP = "top"
    BOTTOM = "bottom"
    ACC = "acc"
    SHOES = "shoes"


class Product(BaseWithUuidId):
    __tablename__ = "product"

    # non-default (required-in-init) first
    url: Mapped[str] = mapped_column(Text, nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brand.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_key: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[ProductCategory] = mapped_column(
        Enum(
            ProductCategory,
            name="product_category",
            native_enum=True,
            create_type=False,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
    )
    original_price: Mapped[int] = mapped_column(Integer, nullable=False)

    # default / nullable fields last
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    image_keys: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default_factory=list,
    )
    subcategory: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    sub_category_detail: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    styles: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default_factory=list,
    )
    fit: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    color: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    pattern: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    material: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default_factory=list,
    )
    details: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default_factory=list,
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default_factory=list,
    )
    discounted_price: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # discount_rate — GENERATED ALWAYS AS ... STORED (read-only from ORM)
    discount_rate: Mapped[Decimal | None] = mapped_column(
        Numeric, nullable=True, init=False, default=None
    )
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # search_vector (tsvector, GENERATED) is not mapped — FTS paths use raw SQL.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True, default=None)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    is_soldout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # Discover "Popular" 탭 정렬 키. data pipeline이 채워 넣음.
    popularity: Mapped[float] = mapped_column(
        DOUBLE_PRECISION, nullable=False, server_default=text("0"), default=0.0
    )
    synced: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
