"""ORM row → DTO mapping helpers for discover / wishlist routes.

Pure functions; no "service" indirection. Route handlers import these directly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.s3 import get_s3
from app.models import Brand, Product, Wishlist
from app.schemas.discover import (
    BrandFull,
    BrandSummary,
    ProductDetail,
    ProductListItem,
    ProductStats,
)


def build_image_url(key: str | None) -> str | None:
    if key is None:
        return None
    return get_s3().public_url(key)


def build_image_urls(keys: list[str] | None) -> list[str]:
    if not keys:
        return []
    s3 = get_s3()
    return [s3.public_url(k) for k in keys]


def discount_rate_to_float(value: Decimal | float | int | None) -> float:
    if value is None:
        return 0.0
    return round(float(value), 4)


def to_brand_summary(brand: Brand) -> BrandSummary:
    return BrandSummary(
        id=brand.id,
        slug=brand.slug,
        name=brand.name,
        name_ko=brand.name_ko,
        logo_url=build_image_url(brand.logo_key),
        platform=brand.platform,
    )


def to_brand_full(
    brand: Brand,
    *,
    is_followed: bool = False,
    product_added: datetime | None = None,
) -> BrandFull:
    return BrandFull(
        id=brand.id,
        slug=brand.slug,
        name=brand.name,
        name_ko=brand.name_ko,
        logo_url=build_image_url(brand.logo_key),
        platform=brand.platform,
        description=brand.description,
        website_url=brand.website_url,
        is_followed=is_followed,
        product_added=product_added,
    )


def to_list_item(
    product: Product,
    brand: Brand,
    *,
    is_wishlisted: bool = False,
    rank: int | None = None,
    saved_at: datetime | None = None,
) -> ProductListItem:
    return ProductListItem(
        id=product.id,
        name=product.name,
        description=product.description,
        brand=to_brand_summary(brand),
        category=product.category,
        subcategory=product.subcategory,
        sub_category_detail=product.sub_category_detail,
        fit=product.fit,
        color=product.color,
        pattern=product.pattern,
        material=list(product.material or []),
        details=list(product.details or []),
        styles=list(product.styles or []),
        tags=list(product.tags or []),
        thumbnail_url=build_image_url(product.thumbnail_key) or "",
        image_urls=build_image_urls(product.image_keys),
        url=product.url,
        original_price=product.original_price,
        discounted_price=product.discounted_price,
        discount_rate=discount_rate_to_float(product.discount_rate),
        is_soldout=product.is_soldout,
        is_active=product.is_active,
        is_wishlisted=is_wishlisted,
        created=product.created,
        rank=rank,
        saved_at=saved_at,
    )


def to_product_detail(
    product: Product,
    brand: Brand,
    *,
    is_wishlisted: bool,
    is_followed: bool,
    stats: ProductStats,
) -> ProductDetail:
    return ProductDetail(
        id=product.id,
        name=product.name,
        description=product.description,
        brand=to_brand_full(brand, is_followed=is_followed),
        category=product.category,
        subcategory=product.subcategory,
        sub_category_detail=product.sub_category_detail,
        fit=product.fit,
        color=product.color,
        pattern=product.pattern,
        material=list(product.material or []),
        details=list(product.details or []),
        styles=list(product.styles or []),
        tags=list(product.tags or []),
        thumbnail_url=build_image_url(product.thumbnail_key) or "",
        image_urls=build_image_urls(product.image_keys),
        url=product.url,
        original_price=product.original_price,
        discounted_price=product.discounted_price,
        discount_rate=discount_rate_to_float(product.discount_rate),
        is_soldout=product.is_soldout,
        is_active=product.is_active,
        is_wishlisted=is_wishlisted,
        created=product.created,
        stats=stats,
    )


async def fetch_wishlisted_ids(
    db: AsyncSession,
    user_id: UUID | None,
    product_ids: list[UUID],
) -> set[UUID]:
    """Return the subset of product_ids that `user_id` has wishlisted.

    Guest users / empty input → empty set.
    """
    if user_id is None or not product_ids:
        return set()
    stmt = select(Wishlist.product_id).where(
        Wishlist.user_id == user_id,
        Wishlist.product_id.in_(product_ids),
    )
    rows = (await db.execute(stmt)).all()
    return {r[0] for r in rows}
