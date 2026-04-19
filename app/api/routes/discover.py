"""Discover feature routes — Popular/New, ForYou, Following, Detail, Related.

SQL queries live directly in route handlers via SQLAlchemy `select()`.
Only `GET /products/{id}` delegates to a service (for the view-event side effect).

Route ordering: concrete paths (`/products/foryou`, `/products/following`)
MUST precede `/products/{product_id}`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, OptionalUser
from app.api.routes._product_mapping import (
    fetch_wishlisted_ids,
    to_brand_full,
    to_list_item,
)
from app.core.envelope import StandardResponseMeta, StandardSuccessResponse
from app.core.errors import AppErrorCodeEnum, AppException
from app.core.request_id import current_request_id
from app.models import Brand, BrandFollow, Product, User, UserProductAffinity
from app.schemas.discover import (
    FollowingBrandBlock,
    FollowingResponse,
    ForYouQuery,
    ForYouResponse,
    ProductDetail,
    ProductFeedQuery,
    ProductListItem,
    ProductListResponse,
    RelatedResponse,
)
from app.services import product as product_service

logger = logging.getLogger("gyeol.discover")

router = APIRouter(tags=["discover"])


# ---------- GET /products/foryou ----------


@router.get(
    "/products/foryou",
    response_model=StandardSuccessResponse[ForYouResponse],
    name="discover.foryou",
)
async def _(
    query: Annotated[ForYouQuery, Depends()],
    db: DbSession,
    user: CurrentUser,
) -> StandardSuccessResponse[ForYouResponse]:
    # popular_fallback cursor가 있으면 바로 fallback 모드.
    if query.cursor_popularity is not None and query.cursor_id is not None:
        items, next_params = await _fetch_popular_feed(
            db,
            user,
            cursor_popularity=query.cursor_popularity,
            cursor_id=query.cursor_id,
            limit=query.limit,
            include_tab_in_next=False,
        )
        return StandardSuccessResponse(
            data=ForYouResponse(mode="popular_fallback", items=items),
            meta=StandardResponseMeta(request_id=current_request_id(), next=next_params),
        )

    # Affinity path.
    stmt = (
        select(Product, Brand, UserProductAffinity.rank)
        .join(Product, Product.id == UserProductAffinity.product_id)
        .join(Brand, Brand.id == Product.brand_id)
        .where(
            UserProductAffinity.user_id == user.id,
            Product.is_active,
        )
        .order_by(UserProductAffinity.rank.asc())
        .limit(query.limit + 1)
    )
    if query.cursor_rank is not None:
        stmt = stmt.where(UserProductAffinity.rank > query.cursor_rank)

    rows = (await db.execute(stmt)).all()
    is_first_page = query.cursor_rank is None

    # 첫 페이지이면서 affinity 0건 → popular fallback.
    if is_first_page and not rows:
        logger.info("foryou.fallback.popular user_id=%s", user.id)
        items, next_params = await _fetch_popular_feed(
            db,
            user,
            cursor_popularity=None,
            cursor_id=None,
            limit=query.limit,
            include_tab_in_next=False,
        )
        return StandardSuccessResponse(
            data=ForYouResponse(mode="popular_fallback", items=items),
            meta=StandardResponseMeta(request_id=current_request_id(), next=next_params),
        )

    has_next = len(rows) > query.limit
    page_rows = rows[: query.limit]

    wishlisted = await fetch_wishlisted_ids(db, user.id, [r.Product.id for r in page_rows])
    items = [
        to_list_item(
            r.Product,
            r.Brand,
            is_wishlisted=r.Product.id in wishlisted,
            rank=int(r.rank),
        )
        for r in page_rows
    ]

    next_params: dict[str, Any] | None = None
    if has_next:
        next_params = {"cursor_rank": int(page_rows[-1].rank)}

    logger.info("foryou.affinity.hit user_id=%s count=%d", user.id, len(page_rows))
    return StandardSuccessResponse(
        data=ForYouResponse(mode="affinity", items=items),
        meta=StandardResponseMeta(request_id=current_request_id(), next=next_params),
    )


# ---------- GET /products/following ----------


@router.get(
    "/products/following",
    response_model=StandardSuccessResponse[FollowingResponse],
    name="discover.following",
)
async def _(
    db: DbSession,
    user: CurrentUser,
) -> StandardSuccessResponse[FollowingResponse]:
    recent_cutoff = datetime.now(UTC) - timedelta(days=7)

    # 1. 팔로우 브랜드별 집계 (7일 내 신상 있는 브랜드만).
    agg_sq = (
        select(
            Product.brand_id.label("brand_id"),
            func.count(Product.id).label("new_count"),
            func.max(Product.created).label("product_added"),
        )
        .where(Product.is_active, Product.created >= recent_cutoff)
        .group_by(Product.brand_id)
        .subquery()
    )

    brand_stmt = (
        select(Brand, agg_sq.c.new_count, agg_sq.c.product_added)
        .join(BrandFollow, BrandFollow.brand_id == Brand.id)
        .join(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(
            BrandFollow.user_id == user.id,
            Brand.is_active,
            agg_sq.c.new_count > 0,
        )
        .order_by(agg_sq.c.product_added.desc(), Brand.id.desc())
    )
    brand_rows = (await db.execute(brand_stmt)).all()

    if not brand_rows:
        return StandardSuccessResponse(
            data=FollowingResponse(total_new=0, brands=[]),
            meta=StandardResponseMeta(request_id=current_request_id()),
        )

    brand_ids = [br.Brand.id for br in brand_rows]

    # 2. 브랜드별 최대 6개 신상 (row_number OVER PARTITION BY).
    rn = (
        func.row_number()
        .over(
            partition_by=Product.brand_id,
            order_by=Product.id.desc(),
        )
        .label("rn")
    )
    ranked_sq = (
        select(Product.id.label("id"), Product.brand_id.label("brand_id"), rn)
        .where(
            Product.brand_id.in_(brand_ids),
            Product.is_active,
            Product.created >= recent_cutoff,
        )
        .subquery()
    )
    products_stmt = (
        select(Product, ranked_sq.c.brand_id)
        .join(ranked_sq, ranked_sq.c.id == Product.id)
        .where(ranked_sq.c.rn <= 6)
        .order_by(ranked_sq.c.brand_id, ranked_sq.c.rn)
    )
    product_rows = (await db.execute(products_stmt)).all()

    product_ids = [p.id for p, _ in product_rows]
    wishlisted = await fetch_wishlisted_ids(db, user.id, product_ids)

    # 3. Group by brand. Reuse the Brand ORM object from brand_stmt.
    brand_map: dict[UUID, Brand] = {br.Brand.id: br.Brand for br in brand_rows}
    items_by_brand: dict[UUID, list[ProductListItem]] = {bid: [] for bid in brand_ids}
    for product, brand_id in product_rows:
        items_by_brand[brand_id].append(
            to_list_item(
                product,
                brand_map[brand_id],
                is_wishlisted=product.id in wishlisted,
            )
        )

    blocks: list[FollowingBrandBlock] = []
    total_new = 0
    for br in brand_rows:
        bid = br.Brand.id
        items = items_by_brand.get(bid, [])
        if not items:
            # Shouldn't happen (agg guarantees new_count > 0) but guard race.
            continue
        brand_full = to_brand_full(
            br.Brand,
            is_followed=True,
            product_added=br.product_added,
        )
        new_count = int(br.new_count)
        total_new += new_count
        blocks.append(FollowingBrandBlock(brand=brand_full, new_count=new_count, items=items))

    return StandardSuccessResponse(
        data=FollowingResponse(total_new=total_new, brands=blocks),
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- GET /products (popular/new) ----------


@router.get(
    "/products",
    response_model=StandardSuccessResponse[ProductListResponse],
    name="discover.feed",
)
async def _(
    query: Annotated[ProductFeedQuery, Depends()],
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[ProductListResponse]:
    if query.tab == "popular":
        items, next_params = await _fetch_popular_feed(
            db,
            user,
            cursor_popularity=query.cursor_popularity,
            cursor_id=query.cursor_id,
            limit=query.limit,
            include_tab_in_next=True,
        )
    else:
        items, next_params = await _fetch_new_feed(
            db, user, cursor_id=query.cursor_id, limit=query.limit
        )

    return StandardSuccessResponse(
        data=ProductListResponse(items=items),
        meta=StandardResponseMeta(request_id=current_request_id(), next=next_params),
    )


# ---------- GET /products/{product_id}/related ----------


@router.get(
    "/products/{product_id}/related",
    response_model=StandardSuccessResponse[RelatedResponse],
    name="discover.related",
)
async def _(
    product_id: UUID,
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[RelatedResponse]:
    related_limit = 4

    seed = (
        await db.execute(
            select(Product.brand_id, Product.embedding).where(
                Product.id == product_id, Product.is_active
            )
        )
    ).one_or_none()
    if seed is None:
        raise AppException(
            AppErrorCodeEnum.PRODUCT_NOT_FOUND,
            detail_payload={"product_id": str(product_id)},
        )
    seed_brand_id, seed_embedding = seed

    base_stmt = (
        select(Product, Brand)
        .join(Brand, Brand.id == Product.brand_id)
        .where(
            Product.id != product_id,
            Product.is_active,
            Brand.is_active,
        )
    )

    if seed_embedding is None:
        # Degraded: same brand priority → id DESC.
        stmt = base_stmt.order_by(
            (Product.brand_id == seed_brand_id).desc(),
            Product.id.desc(),
        ).limit(related_limit)
    else:
        stmt = (
            base_stmt.where(Product.embedding.is_not(None))
            .order_by(
                (Product.brand_id == seed_brand_id).desc(),
                Product.embedding.cosine_distance(seed_embedding),
                Product.id.desc(),
            )
            .limit(related_limit)
        )

    rows = (await db.execute(stmt)).all()
    wishlisted = await fetch_wishlisted_ids(db, user.id if user else None, [p.id for p, _ in rows])
    items = [to_list_item(p, b, is_wishlisted=p.id in wishlisted) for p, b in rows]
    return StandardSuccessResponse(
        data=RelatedResponse(items=items),
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- GET /products/{product_id} ----------


@router.get(
    "/products/{product_id}",
    response_model=StandardSuccessResponse[ProductDetail],
    name="discover.detail",
)
async def _(
    product_id: UUID,
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[ProductDetail]:
    detail = await product_service.get_detail(db, user, product_id)
    return StandardSuccessResponse(
        data=detail,
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- Internal helpers (shared between Popular handler + ForYou fallback) ----------


async def _fetch_popular_feed(
    db: AsyncSession,
    user: User | None,
    *,
    cursor_popularity: float | None,
    cursor_id: UUID | None,
    limit: int,
    include_tab_in_next: bool,
) -> tuple[list[ProductListItem], dict[str, Any] | None]:
    """Popular 피드 1 페이지. ForYou popular_fallback에서도 재사용.

    `include_tab_in_next` — Popular 엔드포인트 응답 next에는 tab="popular" 포함,
    ForYou fallback next에는 tab 키 제외 (ForYou는 tab 쿼리 파라미터가 없음).
    """
    stmt = (
        select(Product, Brand)
        .join(Brand, Brand.id == Product.brand_id)
        .where(Product.is_active, Brand.is_active)
        .order_by(Product.popularity.desc(), Product.id.desc())
        .limit(limit + 1)
    )
    if cursor_popularity is not None and cursor_id is not None:
        stmt = stmt.where(
            tuple_(Product.popularity, Product.id)
            < tuple_(literal(cursor_popularity), literal(cursor_id))
        )

    rows = (await db.execute(stmt)).all()
    has_next = len(rows) > limit
    page_rows = rows[:limit]

    wishlisted = await fetch_wishlisted_ids(
        db, user.id if user else None, [p.id for p, _ in page_rows]
    )
    items = [to_list_item(p, b, is_wishlisted=p.id in wishlisted) for p, b in page_rows]

    next_params: dict[str, Any] | None = None
    if has_next:
        last_product, _ = page_rows[-1]
        next_params = {
            "cursor_popularity": float(last_product.popularity),
            "cursor_id": str(last_product.id),
        }
        if include_tab_in_next:
            next_params["tab"] = "popular"

    return items, next_params


async def _fetch_new_feed(
    db: AsyncSession,
    user: User | None,
    *,
    cursor_id: UUID | None,
    limit: int,
) -> tuple[list[ProductListItem], dict[str, Any] | None]:
    stmt = (
        select(Product, Brand)
        .join(Brand, Brand.id == Product.brand_id)
        .where(Product.is_active, Brand.is_active)
        .order_by(Product.id.desc())
        .limit(limit + 1)
    )
    if cursor_id is not None:
        stmt = stmt.where(Product.id < cursor_id)

    rows = (await db.execute(stmt)).all()
    has_next = len(rows) > limit
    page_rows = rows[:limit]

    wishlisted = await fetch_wishlisted_ids(
        db, user.id if user else None, [p.id for p, _ in page_rows]
    )
    items = [to_list_item(p, b, is_wishlisted=p.id in wishlisted) for p, b in page_rows]

    next_params: dict[str, Any] | None = None
    if has_next:
        last_product, _ = page_rows[-1]
        next_params = {"tab": "new", "cursor_id": str(last_product.id)}

    return items, next_params
