"""ORM row → DTO mapping helpers for brand routes.

Pure functions; no "service" indirection. Route handlers import these directly.
Mirrors the discover `_product_mapping.py` pattern.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes._product_mapping import build_image_url
from app.models import Brand, BrandFollow
from app.schemas.brand import BrandDetailResponse, BrandIndexItem


def to_brand_index_item(
    brand: Brand,
    *,
    product_count: int,
    new_count: int,
    is_followed: bool = False,
) -> BrandIndexItem:
    return BrandIndexItem(
        id=brand.id,
        slug=brand.slug,
        name=brand.name,
        name_ko=brand.name_ko,
        logo_url=build_image_url(brand.logo_key),
        cover_url=build_image_url(brand.cover_key),
        platform=brand.platform,
        styles=list(brand.styles or []),
        is_featured=brand.is_featured,
        is_followed=is_followed,
        product_count=product_count,
        new_count=new_count,
        product_added=brand.product_added,
    )


def to_brand_detail(
    brand: Brand,
    *,
    product_count: int,
    new_count: int,
    is_followed: bool,
    related_brands: list[BrandIndexItem],
) -> BrandDetailResponse:
    return BrandDetailResponse(
        id=brand.id,
        slug=brand.slug,
        name=brand.name,
        name_ko=brand.name_ko,
        logo_url=build_image_url(brand.logo_key),
        cover_url=build_image_url(brand.cover_key),
        platform=brand.platform,
        styles=list(brand.styles or []),
        is_featured=brand.is_featured,
        is_followed=is_followed,
        product_count=product_count,
        new_count=new_count,
        product_added=brand.product_added,
        description=brand.description,
        website_url=brand.website_url,
        related_brands=related_brands,
    )


async def fetch_followed_brand_ids(
    db: AsyncSession,
    user_id: UUID | None,
    brand_ids: list[UUID],
) -> set[UUID]:
    """Return the subset of brand_ids that `user_id` has followed.

    Guest users / empty input → empty set.
    """
    if user_id is None or not brand_ids:
        return set()
    stmt = select(BrandFollow.brand_id).where(
        BrandFollow.user_id == user_id,
        BrandFollow.brand_id.in_(brand_ids),
    )
    rows = (await db.execute(stmt)).all()
    return {r[0] for r in rows}
