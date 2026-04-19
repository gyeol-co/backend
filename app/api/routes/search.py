"""Search feature routes — hybrid search + suggestions.

스펙: app/backend/specs/search.md. Two endpoints:

- `GET /search` — products (cursor-paginated, hybrid) + brands (top-5 fixed).
  Public; login users get `is_wishlisted` / `is_followed` enriched and a
  `search_event` row on first-page valid queries.
- `GET /search/suggestions` — empty-state hint. Login users get `recent`
  (last 5 distinct, 30d window); `suggestions` is the hardcoded fixed list.

Route handler stays thin — hybrid pipeline + brand FTS orchestration lives
in `services.search`. The handler drives `asyncio.gather` for products +
brands parallel execution and fires the `search_event` side-effect.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, OptionalUser
from app.constants.search import FIXED_SUGGESTIONS
from app.core.envelope import StandardResponseMeta, StandardSuccessResponse
from app.core.request_id import current_request_id
from app.schemas.search import (
    SearchProductsSection,
    SearchQuery,
    SearchResponse,
    SuggestionsResponse,
)
from app.services import search as search_service

logger = logging.getLogger("gyeol.search")

router = APIRouter(prefix="/search", tags=["search"])


# ---------- GET /search/suggestions ----------
# Define before `GET /search` so the more specific path is matched — FastAPI
# resolves by route-definition order when paths share a prefix.


@router.get(
    "/suggestions",
    response_model=StandardSuccessResponse[SuggestionsResponse],
    name="search.suggestions",
)
async def _(
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[SuggestionsResponse]:
    recent = await search_service.get_recent_queries(db, user.id) if user is not None else []
    return StandardSuccessResponse(
        data=SuggestionsResponse(recent=recent, suggestions=list(FIXED_SUGGESTIONS)),
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- GET /search ----------


@router.get(
    "",
    response_model=StandardSuccessResponse[SearchResponse],
    name="search.query",
)
async def _(
    # Pydantic BaseModel bound as Query (FastAPI 0.115+). Query() wires
    # `style` as a repeat-encoded list[str] (matches brand index); Depends()
    # would swallow list fields.
    query: Annotated[SearchQuery, Query()],
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[SearchResponse]:
    # Sequential — both services hit the same AsyncSession.
    products_result = await search_service.run_products_search(db, user, query)
    brand_items = await search_service.run_brands_search(db, user, query.q)

    # Step 6 — log search_event (login + first page only).
    if user is not None and query.is_first_page():
        await search_service.log_search_event(
            db, user.id, query.q, products_result.result_count
        )

    return StandardSuccessResponse(
        data=SearchResponse(
            query=query.q,
            sort=query.sort,
            products=SearchProductsSection(
                items=products_result.items,
                total_candidates=products_result.total_candidates,
            ),
            brands=brand_items,
            meta_info=products_result.meta_info,
        ),
        meta=StandardResponseMeta(
            request_id=current_request_id(),
            next=products_result.next_cursor,
        ),
    )
