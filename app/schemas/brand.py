"""Brand feature DTOs — request queries + response envelopes.

스펙: app/backend/specs/brand.md (§Shared Concerns §Pydantic DTOs).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.product import ProductCategory
from app.schemas.discover import ProductListItem

BrandIndexSort = Literal["alpha", "featured", "recent"]


# ---- Request queries ----


class BrandIndexQuery(BaseModel):
    """GET /brands — sort/style/cursor 조합 전체.

    Wire format은 "cursor_product_added 키 없음" vs "키 있고 값" 2 가지뿐.
    tail phase(NULLS LAST, product_added IS NULL) 진입은 `cursor_product_added` 키를
    생략하고 `cursor_id` 만 전달하는 요청으로 표현한다. Pydantic 필드는 default=None
    이므로 "키 생략" == "None" 으로 자연 매핑된다.
    """

    model_config = ConfigDict(extra="forbid")

    sort: BrandIndexSort = "alpha"
    # FastAPI Depends() 로 query binding 시 Field(default_factory=...) 는
    # pydantic-core 에 `_HAS_DEFAULT_FACTORY_CLASS` sentinel 을 흘려 validation
    # 실패를 일으킨다. default=[] 로 두면 pydantic 이 매 인스턴스마다 copy 한다.
    style: list[str] = []
    limit: int = Field(default=24, ge=1, le=40)

    # alpha 전용
    cursor_name: str | None = None
    # recent / featured
    cursor_product_added: datetime | None = None
    # featured 전용
    cursor_is_featured: bool | None = None
    # 공통 tie-breaker
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> BrandIndexQuery:
        if self.sort == "alpha":
            if (self.cursor_name is None) != (self.cursor_id is None):
                raise ValueError("cursor_name and cursor_id must both be present or absent")
            if self.cursor_product_added is not None or self.cursor_is_featured is not None:
                raise ValueError(
                    "cursor_product_added / cursor_is_featured not valid for sort=alpha"
                )
        elif self.sort == "recent":
            if self.cursor_is_featured is not None:
                raise ValueError("cursor_is_featured not valid for sort=recent")
            if self.cursor_name is not None:
                raise ValueError("cursor_name not valid for sort=recent")
            # (cursor_product_added, cursor_id) — 둘 다 없거나, cursor_id만(tail),
            # 둘 다 있거나 셋 중 하나. cursor_product_added만 있고 cursor_id 없는 건 invalid.
            if self.cursor_product_added is not None and self.cursor_id is None:
                raise ValueError("cursor_product_added requires cursor_id")
        else:  # featured
            if self.cursor_name is not None:
                raise ValueError("cursor_name not valid for sort=featured")
            # featured cursor: cursor_is_featured 있으면 cursor_id도 반드시 있어야 함
            if self.cursor_is_featured is not None and self.cursor_id is None:
                raise ValueError("cursor_is_featured requires cursor_id")
            if self.cursor_product_added is not None and self.cursor_is_featured is None:
                raise ValueError(
                    "cursor_product_added requires cursor_is_featured for sort=featured"
                )
        return self


class BrandProductsQuery(BaseModel):
    """GET /brands/{slug}/products."""

    model_config = ConfigDict(extra="forbid")

    category: ProductCategory | None = None
    limit: int = Field(default=24, ge=1, le=40)
    cursor_popularity: float | None = None
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> BrandProductsQuery:
        both_present = self.cursor_popularity is not None and self.cursor_id is not None
        both_absent = self.cursor_popularity is None and self.cursor_id is None
        if not (both_present or both_absent):
            raise ValueError("cursor_popularity and cursor_id must both be present or absent")
        return self


# ---- Response DTOs ----


class BrandIndexItem(BaseModel):
    """Index / Detail / Related 공통 rich 브랜드 카드.

    `discover.BrandSummary`와 구분되는 점:
    - cover_url 추가
    - is_featured 추가
    - styles (array) 추가
    - product_count / new_count 집계 추가
    - product_added (브랜드 row) 그대로 노출
    - is_followed — 로그인 시 채움
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    name_ko: str | None = None
    logo_url: str | None = None
    cover_url: str | None = None
    platform: str | None = None
    styles: list[str]
    is_featured: bool
    is_followed: bool = False
    product_count: int
    new_count: int
    product_added: datetime | None = None


class BrandIndexResponse(BaseModel):
    items: list[BrandIndexItem]


class BrandDetailResponse(BrandIndexItem):
    """Detail = Index item + description/website_url + related embed."""

    description: str | None = None
    website_url: str | None = None
    related_brands: list[BrandIndexItem]


class BrandProductsResponse(BaseModel):
    items: list[ProductListItem]
    count_total: int
    count_by_category: dict[str, int]


class BrandFollowResponse(BaseModel):
    """POST/DELETE 공용. is_followed 만 반대."""

    model_config = ConfigDict(from_attributes=True)

    brand_id: UUID
    slug: str
    is_followed: bool
    follower_count: int
