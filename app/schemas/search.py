"""Search feature DTOs — request queries + response envelopes.

스펙: app/backend/specs/search.md §Shared Concerns §Pydantic DTOs.
FE 계약: docs/plans/search/api-mapping.md §B.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.product import ProductCategory
from app.schemas.brand import BrandIndexItem
from app.schemas.discover import ProductListItem

SearchSort = Literal["relevance", "recent", "price_asc", "price_desc"]
SearchDegradedReason = Literal[
    "embedding_unavailable",
    "sort_not_relevance",
]


# ---- Request queries ----


class SearchQuery(BaseModel):
    """GET /search query parameters.

    `style` wire param is singular + repeat-encoded (`?style=a&style=b`) to
    match brand/discover convention. Pydantic field name is `style` (list[str]);
    DB column is plural `product.styles` — the service maps.

    Cursor fields are sort-scoped — validator enforces sort/cursor consistency
    and raises ValidationError on mismatch.
    """

    model_config = ConfigDict(extra="forbid")

    q: str = Field(max_length=80)
    sort: SearchSort = "relevance"
    category: ProductCategory | None = None
    style: list[str] = []
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    limit: int = Field(default=24, ge=1, le=40)

    # Sort-scoped cursor fields. Exactly one sort's keyset pair may be present.
    cursor_score: float | None = None
    cursor_created: datetime | None = None
    cursor_price: int | None = None
    cursor_id: UUID | None = None

    @field_validator("q", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> SearchQuery:
        if len(self.q) < 2:
            raise ValueError("q must be at least 2 characters after strip")
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("price_min must be <= price_max")

        if self.sort == "relevance":
            if self.cursor_created is not None or self.cursor_price is not None:
                raise ValueError("only cursor_score/cursor_id valid for sort=relevance")
            if (self.cursor_score is None) != (self.cursor_id is None):
                raise ValueError("cursor_score and cursor_id must both be present or absent")
        elif self.sort == "recent":
            if self.cursor_score is not None or self.cursor_price is not None:
                raise ValueError("only cursor_created/cursor_id valid for sort=recent")
            if (self.cursor_created is None) != (self.cursor_id is None):
                raise ValueError("cursor_created and cursor_id must both be present or absent")
        else:  # price_asc | price_desc
            if self.cursor_score is not None or self.cursor_created is not None:
                raise ValueError("only cursor_price/cursor_id valid for sort=price_*")
            if (self.cursor_price is None) != (self.cursor_id is None):
                raise ValueError("cursor_price and cursor_id must both be present or absent")
        return self

    def is_first_page(self) -> bool:
        """True when no cursor field is set — used to gate search_event insert."""
        return (
            self.cursor_score is None
            and self.cursor_created is None
            and self.cursor_price is None
            and self.cursor_id is None
        )


# ---- Response DTOs ----


class SearchProductsSection(BaseModel):
    items: list[ProductListItem]
    total_candidates: int


class SearchMetaInfo(BaseModel):
    """Pipeline stage observability. FE doesn't render directly, but
    `degraded_reason === 'embedding_unavailable'` drives a 1-time toast.
    """

    fts_applied: bool
    embedding_applied: bool
    degraded_reason: SearchDegradedReason | None = None


class SearchResponse(BaseModel):
    query: str
    sort: SearchSort
    products: SearchProductsSection
    brands: list[BrandIndexItem]
    meta_info: SearchMetaInfo


class SuggestionsResponse(BaseModel):
    """GET /search/suggestions.

    Response JSON keys: `recent` + `suggestions`. Internal constant name for
    the fixed list is `FIXED_SUGGESTIONS` (see app/constants/search.py).
    """

    recent: list[str]
    suggestions: list[str]
