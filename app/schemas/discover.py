"""Discover feature DTOs — request queries + response envelopes.

스펙: app/backend/specs/discover.md (Shared Concerns §Pydantic DTOs).
FE 계약: docs/plans/discover/api-mapping.md §B.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.product import ProductCategory

# ---- Request queries (FastAPI Depends() bound) ----


class ProductFeedQuery(BaseModel):
    """GET /products — popular/new 탭 공용."""

    model_config = ConfigDict(extra="forbid")

    tab: Literal["popular", "new"]
    limit: int = Field(default=20, ge=1, le=40)
    cursor_popularity: float | None = None
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check_cursor(self) -> ProductFeedQuery:
        if self.tab == "new" and self.cursor_popularity is not None:
            raise ValueError("cursor_popularity is only valid when tab=popular")
        if self.tab == "popular":
            both_present = self.cursor_popularity is not None and self.cursor_id is not None
            both_absent = self.cursor_popularity is None and self.cursor_id is None
            if not (both_present or both_absent):
                raise ValueError(
                    "cursor_popularity and cursor_id must both be present or both absent"
                )
        return self


class ForYouQuery(BaseModel):
    """GET /products/foryou.

    affinity 모드 cursor: cursor_rank.
    popular_fallback 모드 cursor: cursor_popularity + cursor_id (Popular과 동일).
    FE가 mode에 맞는 키만 실어 보낸다.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=40)
    cursor_rank: int | None = Field(default=None, ge=0)
    cursor_popularity: float | None = None
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check_cursor(self) -> ForYouQuery:
        # popular_fallback cursor 쌍은 함께 제공.
        both_present = self.cursor_popularity is not None and self.cursor_id is not None
        both_absent = self.cursor_popularity is None and self.cursor_id is None
        if not (both_present or both_absent):
            raise ValueError("cursor_popularity and cursor_id must both be present or both absent")
        # affinity cursor와 popular_fallback cursor는 동시에 제공될 수 없음.
        if self.cursor_rank is not None and both_present:
            raise ValueError("cursor_rank cannot be combined with cursor_popularity/cursor_id")
        return self


class WishlistListQuery(BaseModel):
    """GET /wishlist."""

    model_config = ConfigDict(extra="forbid")

    category: ProductCategory | None = None
    skip: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=100, ge=1, le=100)


# ---- Response DTOs ----


class BrandSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    name_ko: str | None = None
    logo_url: str | None = None
    platform: str | None = None


class BrandFull(BrandSummary):
    description: str | None = None
    website_url: str | None = None
    is_followed: bool = False
    # Following 섹션 헤더 전용 (Detail에선 None).
    product_added: datetime | None = None


class ProductListItem(BaseModel):
    """Popular/New/ForYou/Following/Related/Wishlist 공용 rich 리스트 아이템."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    brand: BrandSummary
    category: ProductCategory
    subcategory: str | None = None
    sub_category_detail: str | None = None
    fit: str | None = None
    color: str | None = None
    pattern: str | None = None
    material: list[str]
    details: list[str]
    styles: list[str]
    tags: list[str]
    thumbnail_url: str
    image_urls: list[str]
    url: str
    original_price: int
    discounted_price: int | None = None
    discount_rate: float
    is_soldout: bool
    is_active: bool
    is_wishlisted: bool = False
    created: datetime
    # 엔드포인트별 선택.
    rank: int | None = None
    saved_at: datetime | None = None


class ProductStats(BaseModel):
    view_count_7d: int
    wishlist_count: int


class ProductDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None
    brand: BrandFull
    category: ProductCategory
    subcategory: str | None = None
    sub_category_detail: str | None = None
    fit: str | None = None
    color: str | None = None
    pattern: str | None = None
    material: list[str]
    details: list[str]
    styles: list[str]
    tags: list[str]
    thumbnail_url: str
    image_urls: list[str]
    url: str
    original_price: int
    discounted_price: int | None = None
    discount_rate: float
    is_soldout: bool
    is_active: bool
    is_wishlisted: bool = False
    created: datetime
    stats: ProductStats


class ProductListResponse(BaseModel):
    items: list[ProductListItem]


class ForYouResponse(BaseModel):
    mode: Literal["affinity", "popular_fallback"]
    items: list[ProductListItem]


class FollowingBrandBlock(BaseModel):
    brand: BrandFull
    new_count: int
    items: list[ProductListItem]


class FollowingResponse(BaseModel):
    total_new: int
    brands: list[FollowingBrandBlock]


class RelatedResponse(BaseModel):
    items: list[ProductListItem]


class WishlistListResponse(BaseModel):
    count_total: int
    count_by_category: dict[str, int]
    items: list[ProductListItem]


class WishlistToggleResponse(BaseModel):
    product_id: UUID
    is_wishlisted: bool
    wishlist_count: int
