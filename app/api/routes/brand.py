"""Brand feature routes — Index, Detail, Products, Follow, Unfollow.

스펙: app/backend/specs/brand.md.

SQL queries live directly in route handlers via SQLAlchemy `select()`.
Only follow/unfollow mutations delegate to `services.brand_follow` for
dual-write (brand_follow row + brand_event row).

Route ordering: concrete paths (`/brands/{slug}/follow`, `/brands/{slug}/products`)
MUST precede `/brands/{slug}`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ARRAY, Text, cast, func, literal, select, tuple_
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, OptionalUser
from app.api.routes._brand_mapping import (
    fetch_followed_brand_ids,
    to_brand_detail,
    to_brand_index_item,
)
from app.api.routes._product_mapping import fetch_wishlisted_ids, to_list_item
from app.core.envelope import StandardResponseMeta, StandardSuccessResponse
from app.core.errors import AppErrorCodeEnum, AppException
from app.core.request_id import current_request_id
from app.models import Brand, BrandFollow, Product, ProductCategory
from app.schemas.brand import (
    BrandDetailResponse,
    BrandFollowResponse,
    BrandIndexItem,
    BrandIndexQuery,
    BrandIndexResponse,
    BrandProductsQuery,
    BrandProductsResponse,
)
from app.schemas.discover import ProductListItem
from app.services import brand_follow as brand_follow_service

logger = logging.getLogger("gyeol.brand")

router = APIRouter(prefix="/brands", tags=["brand"])


# ---------- Shared: aggregate subquery ----------


def _recent_cutoff() -> datetime:
    return datetime.now(UTC) - timedelta(days=7)


def _build_agg_subquery(recent_cutoff: datetime):
    """Per-brand aggregate: product_count + new_count (last 7d)."""
    return (
        select(
            Product.brand_id.label("brand_id"),
            func.count(Product.id)
            .filter(Product.is_active)
            .label("product_count"),
            func.count(Product.id)
            .filter(Product.is_active, Product.created >= recent_cutoff)
            .label("new_count"),
        )
        .group_by(Product.brand_id)
        .subquery()
    )


# ---------- GET /brands (Index) ----------


@router.get(
    "",
    response_model=StandardSuccessResponse[BrandIndexResponse],
    name="brand.index",
)
async def _(
    # Pydantic BaseModel bound as Query (FastAPI 0.115+). Using Depends() would
    # silently swallow list[str] fields; Query() wires each field as a proper
    # repeated query parameter (e.g. `?style=a&style=b`).
    query: Annotated[BrandIndexQuery, Query()],
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[BrandIndexResponse]:
    items, next_params = await _fetch_brand_index(db, user.id if user else None, query)
    return StandardSuccessResponse(
        data=BrandIndexResponse(items=items),
        meta=StandardResponseMeta(request_id=current_request_id(), next=next_params),
    )


# ---------- POST /brands/{slug}/follow ----------


@router.post(
    "/{slug}/follow",
    response_model=StandardSuccessResponse[BrandFollowResponse],
    name="brand.follow",
)
async def _(
    slug: str,
    db: DbSession,
    user: CurrentUser,
) -> StandardSuccessResponse[BrandFollowResponse]:
    brand_id = await brand_follow_service.follow(db, user, slug)
    count = (
        await db.execute(select(func.count()).where(BrandFollow.brand_id == brand_id))
    ).scalar_one()
    return StandardSuccessResponse(
        data=BrandFollowResponse(
            brand_id=brand_id, slug=slug, is_followed=True, follower_count=count
        ),
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- DELETE /brands/{slug}/follow ----------


@router.delete(
    "/{slug}/follow",
    response_model=StandardSuccessResponse[BrandFollowResponse],
    name="brand.unfollow",
)
async def _(
    slug: str,
    db: DbSession,
    user: CurrentUser,
) -> StandardSuccessResponse[BrandFollowResponse]:
    brand_id = await brand_follow_service.unfollow(db, user, slug)
    count = (
        await db.execute(select(func.count()).where(BrandFollow.brand_id == brand_id))
    ).scalar_one()
    return StandardSuccessResponse(
        data=BrandFollowResponse(
            brand_id=brand_id, slug=slug, is_followed=False, follower_count=count
        ),
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- GET /brands/{slug}/products ----------


@router.get(
    "/{slug}/products",
    response_model=StandardSuccessResponse[BrandProductsResponse],
    name="brand.products",
)
async def _(
    slug: str,
    query: Annotated[BrandProductsQuery, Depends()],
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[BrandProductsResponse]:
    # 0. slug → brand_id
    brand_row = (
        await db.execute(
            select(Brand.id, Brand).where(Brand.slug == slug, Brand.is_active)
        )
    ).one_or_none()
    if brand_row is None:
        raise AppException(
            AppErrorCodeEnum.BRAND_NOT_FOUND,
            detail_payload={"slug": slug},
        )
    brand_id = brand_row.id
    brand = brand_row.Brand

    # 1. Page items
    items_stmt = (
        select(Product)
        .where(
            Product.brand_id == brand_id,
            Product.is_active,
        )
        .order_by(Product.popularity.desc(), Product.id.desc())
        .limit(query.limit + 1)
    )
    if query.category is not None:
        items_stmt = items_stmt.where(Product.category == query.category)
    if query.cursor_popularity is not None and query.cursor_id is not None:
        items_stmt = items_stmt.where(
            tuple_(Product.popularity, Product.id)
            < tuple_(literal(query.cursor_popularity), literal(query.cursor_id))
        )

    # 2. counts (filter 무관하게 전체 브랜드 기준)
    count_total_stmt = (
        select(func.count())
        .select_from(Product)
        .where(Product.brand_id == brand_id, Product.is_active)
    )
    count_by_cat_stmt = (
        select(Product.category, func.count().label("cnt"))
        .where(Product.brand_id == brand_id, Product.is_active)
        .group_by(Product.category)
    )

    items_result, total_result, by_cat_result = await asyncio.gather(
        db.execute(items_stmt),
        db.execute(count_total_stmt),
        db.execute(count_by_cat_stmt),
    )

    product_rows = items_result.scalars().all()
    has_next = len(product_rows) > query.limit
    page_rows = product_rows[: query.limit]

    count_total = int(total_result.scalar_one() or 0)
    count_by_category: dict[str, int] = {cat.value: 0 for cat in ProductCategory}
    for cat, cnt in by_cat_result.all():
        count_by_category[cat.value] = int(cnt)

    wishlisted = await fetch_wishlisted_ids(
        db, user.id if user else None, [p.id for p in page_rows]
    )

    items: list[ProductListItem] = [
        to_list_item(p, brand, is_wishlisted=p.id in wishlisted) for p in page_rows
    ]

    next_params: dict[str, Any] | None = None
    if has_next:
        last = page_rows[-1]
        next_params = {
            "cursor_popularity": float(last.popularity),
            "cursor_id": str(last.id),
        }

    logger.info(
        "brand.products.ok slug=%s count=%d count_total=%d",
        slug,
        len(page_rows),
        count_total,
    )
    return StandardSuccessResponse(
        data=BrandProductsResponse(
            items=items,
            count_total=count_total,
            count_by_category=count_by_category,
        ),
        meta=StandardResponseMeta(request_id=current_request_id(), next=next_params),
    )


# ---------- GET /brands/{slug} (Detail) ----------


@router.get(
    "/{slug}",
    response_model=StandardSuccessResponse[BrandDetailResponse],
    name="brand.detail",
)
async def _(
    slug: str,
    db: DbSession,
    user: OptionalUser,
) -> StandardSuccessResponse[BrandDetailResponse]:
    recent_cutoff = _recent_cutoff()
    agg_sq = _build_agg_subquery(recent_cutoff)

    # 1. Seed brand + aggregate
    seed_stmt = (
        select(
            Brand,
            func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
            func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
        )
        .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(Brand.slug == slug, Brand.is_active)
    )
    seed_row = (await db.execute(seed_stmt)).one_or_none()
    if seed_row is None:
        raise AppException(
            AppErrorCodeEnum.BRAND_NOT_FOUND,
            detail_payload={"slug": slug},
        )
    seed_brand = seed_row.Brand
    seed_product_count = int(seed_row.product_count)
    seed_new_count = int(seed_row.new_count)

    # 2. Related brands — same-styles overlap, top 3 by product_added DESC NULLS LAST, id DESC
    related_rows: list[tuple[Brand, int, int]] = []
    if seed_brand.styles:
        related_stmt = (
            select(
                Brand,
                func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
                func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
            )
            .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
            .where(
                Brand.id != seed_brand.id,
                Brand.is_active,
                Brand.styles.op("&&")(
                    cast(pg_array(list(seed_brand.styles)), ARRAY(Text))
                ),
            )
            .order_by(Brand.product_added.desc().nulls_last(), Brand.id.desc())
            .limit(3)
        )
        rows = (await db.execute(related_stmt)).all()
        related_rows = [(r.Brand, int(r.product_count), int(r.new_count)) for r in rows]

    # 3. is_followed batch (seed + related)
    all_brand_ids = [seed_brand.id] + [b.id for b, _, _ in related_rows]
    followed_ids = await fetch_followed_brand_ids(
        db, user.id if user else None, all_brand_ids
    )

    related_items: list[BrandIndexItem] = [
        to_brand_index_item(
            b,
            product_count=pc,
            new_count=nc,
            is_followed=b.id in followed_ids,
        )
        for b, pc, nc in related_rows
    ]

    detail = to_brand_detail(
        seed_brand,
        product_count=seed_product_count,
        new_count=seed_new_count,
        is_followed=seed_brand.id in followed_ids,
        related_brands=related_items,
    )

    logger.info(
        "brand.detail.ok slug=%s related=%d",
        slug,
        len(related_items),
    )
    return StandardSuccessResponse(
        data=detail,
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- Internal: /brands index dispatch ----------


async def _fetch_brand_index(
    db: AsyncSession,
    user_id: UUID | None,
    query: BrandIndexQuery,
) -> tuple[list[BrandIndexItem], dict[str, Any] | None]:
    if query.sort == "alpha":
        rows, has_next = await _fetch_brands_alpha(db, query)
    elif query.sort == "recent":
        rows, has_next = await _fetch_brands_recent(db, query)
    else:
        rows, has_next = await _fetch_brands_featured(db, query)

    brand_ids = [b.id for b, _, _ in rows]
    followed_ids = await fetch_followed_brand_ids(db, user_id, brand_ids)

    items = [
        to_brand_index_item(
            b,
            product_count=pc,
            new_count=nc,
            is_followed=b.id in followed_ids,
        )
        for b, pc, nc in rows
    ]

    next_params: dict[str, Any] | None = None
    if has_next and rows:
        last_brand, _, _ = rows[-1]
        next_params = _build_next_cursor(query.sort, last_brand)

    logger.info(
        "brand.index.ok sort=%s count=%d has_next=%s",
        query.sort,
        len(items),
        has_next,
    )
    return items, next_params


def _build_next_cursor(sort: str, last: Brand) -> dict[str, Any]:
    """Construct meta.next cursor dict for the given sort.

    Keys with null values are OMITTED (§C-2 handshake — wire absence == None).
    """
    if sort == "alpha":
        return {
            "cursor_name": last.name,
            "cursor_id": str(last.id),
        }
    if sort == "recent":
        out: dict[str, Any] = {"cursor_id": str(last.id)}
        if last.product_added is not None:
            out["cursor_product_added"] = last.product_added.isoformat()
        return out
    # featured
    out = {
        "cursor_is_featured": bool(last.is_featured),
        "cursor_id": str(last.id),
    }
    if last.product_added is not None:
        out["cursor_product_added"] = last.product_added.isoformat()
    return out


# ---------- alpha sort ----------


async def _fetch_brands_alpha(
    db: AsyncSession,
    query: BrandIndexQuery,
) -> tuple[list[tuple[Brand, int, int]], bool]:
    """ORDER BY (name ASC, id ASC), tuple keyset cursor."""
    agg_sq = _build_agg_subquery(_recent_cutoff())
    stmt = (
        select(
            Brand,
            func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
            func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
        )
        .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(Brand.is_active)
        .order_by(Brand.name.asc(), Brand.id.asc())
        .limit(query.limit + 1)
    )
    stmt = _apply_style_filter(stmt, query.style)

    if query.cursor_name is not None and query.cursor_id is not None:
        stmt = stmt.where(
            tuple_(Brand.name, Brand.id)
            > tuple_(literal(query.cursor_name), literal(query.cursor_id))
        )

    rows = (await db.execute(stmt)).all()
    has_next = len(rows) > query.limit
    page = rows[: query.limit]
    return [(r.Brand, int(r.product_count), int(r.new_count)) for r in page], has_next


# ---------- recent sort (2-phase NULLS LAST) ----------


async def _fetch_brands_recent(
    db: AsyncSession,
    query: BrandIndexQuery,
) -> tuple[list[tuple[Brand, int, int]], bool]:
    """ORDER BY (product_added DESC NULLS LAST, id DESC).

    2-phase:
    - Phase A: product_added IS NOT NULL, ordered by (product_added DESC, id DESC).
    - Phase B: product_added IS NULL, ordered by (id DESC). Tail.
    """
    # Phase 판정.
    if query.cursor_product_added is not None:
        phase = "A"  # Phase A 후속
    elif query.cursor_id is not None:
        phase = "B"  # Phase B 진입 (null tail)
    else:
        phase = "A"  # 첫 페이지

    if phase == "B":
        rows = await _fetch_recent_phase_b(
            db, query, cursor_id=query.cursor_id, limit=query.limit + 1
        )
        has_next = len(rows) > query.limit
        return rows[: query.limit], has_next

    # Phase A first
    rows_a = await _fetch_recent_phase_a(
        db,
        query,
        cursor_product_added=query.cursor_product_added,
        cursor_id=query.cursor_id,
        limit=query.limit + 1,
    )

    if len(rows_a) > query.limit:
        # Phase A not exhausted this page
        has_next = True
        return rows_a[: query.limit], has_next

    # Phase A exhausted; fill with Phase B
    remaining = query.limit - len(rows_a) + 1
    rows_b = await _fetch_recent_phase_b(db, query, cursor_id=None, limit=remaining)
    combined = rows_a + rows_b
    has_next = len(combined) > query.limit
    return combined[: query.limit], has_next


async def _fetch_recent_phase_a(
    db: AsyncSession,
    query: BrandIndexQuery,
    *,
    cursor_product_added: datetime | None,
    cursor_id: UUID | None,
    limit: int,
) -> list[tuple[Brand, int, int]]:
    agg_sq = _build_agg_subquery(_recent_cutoff())
    stmt = (
        select(
            Brand,
            func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
            func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
        )
        .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(Brand.is_active, Brand.product_added.is_not(None))
        .order_by(Brand.product_added.desc(), Brand.id.desc())
        .limit(limit)
    )
    stmt = _apply_style_filter(stmt, query.style)

    if cursor_product_added is not None and cursor_id is not None:
        stmt = stmt.where(
            tuple_(Brand.product_added, Brand.id)
            < tuple_(literal(cursor_product_added), literal(cursor_id))
        )

    rows = (await db.execute(stmt)).all()
    return [(r.Brand, int(r.product_count), int(r.new_count)) for r in rows]


async def _fetch_recent_phase_b(
    db: AsyncSession,
    query: BrandIndexQuery,
    *,
    cursor_id: UUID | None,
    limit: int,
) -> list[tuple[Brand, int, int]]:
    agg_sq = _build_agg_subquery(_recent_cutoff())
    stmt = (
        select(
            Brand,
            func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
            func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
        )
        .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(Brand.is_active, Brand.product_added.is_(None))
        .order_by(Brand.id.desc())
        .limit(limit)
    )
    stmt = _apply_style_filter(stmt, query.style)

    if cursor_id is not None:
        stmt = stmt.where(Brand.id < cursor_id)

    rows = (await db.execute(stmt)).all()
    return [(r.Brand, int(r.product_count), int(r.new_count)) for r in rows]


# ---------- featured sort (2-phase featured flag + recent-style NULLS LAST) ----------


async def _fetch_brands_featured(
    db: AsyncSession,
    query: BrandIndexQuery,
) -> tuple[list[tuple[Brand, int, int]], bool]:
    """Internal keyset: (is_featured DESC, product_added DESC NULLS LAST, id DESC).

    2-phase: featured=true first, then featured=false. Within each phase, recent-
    style NULLS LAST handling.
    """
    # Phase flag 결정
    # - 첫 페이지: cursor_is_featured 없음 → phase_flag=True
    # - cursor_is_featured 주어짐 → 그 값이 현재 phase
    if query.cursor_is_featured is None:
        phase_flag = True
        sub_phase = "A"  # non-null product_added 먼저
    else:
        phase_flag = query.cursor_is_featured
        # cursor_product_added 있으면 phase A 후속, 없고 cursor_id 있으면 phase B tail
        if query.cursor_product_added is not None:
            sub_phase = "A"
        else:
            sub_phase = "B"

    want = query.limit + 1
    collected: list[tuple[Brand, int, int]] = []

    # Loop over up to 4 sub-phases: (true, A) → (true, B) → (false, A) → (false, B).
    # Bounded loop — max 4 iterations.
    phases: list[tuple[bool, str]] = [
        (True, "A"),
        (True, "B"),
        (False, "A"),
        (False, "B"),
    ]
    # Start index based on current (phase_flag, sub_phase)
    start_idx = phases.index((phase_flag, sub_phase))

    for idx in range(start_idx, len(phases)):
        current_flag, current_sub = phases[idx]
        remaining = want - len(collected)
        if remaining <= 0:
            break

        # Only pass the cursor to the FIRST queried sub-phase (user's current phase).
        # Subsequent sub-phases during overflow fill start from the beginning.
        use_cursor = idx == start_idx

        if current_sub == "A":
            rows = await _fetch_featured_phase_a(
                db,
                query,
                phase_flag=current_flag,
                cursor_product_added=query.cursor_product_added if use_cursor else None,
                cursor_id=query.cursor_id if use_cursor else None,
                limit=remaining,
            )
        else:  # B
            rows = await _fetch_featured_phase_b(
                db,
                query,
                phase_flag=current_flag,
                cursor_id=query.cursor_id if use_cursor else None,
                limit=remaining,
            )

        collected.extend(rows)
        if len(collected) >= want:
            break

    has_next = len(collected) > query.limit
    return collected[: query.limit], has_next


async def _fetch_featured_phase_a(
    db: AsyncSession,
    query: BrandIndexQuery,
    *,
    phase_flag: bool,
    cursor_product_added: datetime | None,
    cursor_id: UUID | None,
    limit: int,
) -> list[tuple[Brand, int, int]]:
    agg_sq = _build_agg_subquery(_recent_cutoff())
    stmt = (
        select(
            Brand,
            func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
            func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
        )
        .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(
            Brand.is_active,
            Brand.is_featured == phase_flag,
            Brand.product_added.is_not(None),
        )
        .order_by(Brand.product_added.desc(), Brand.id.desc())
        .limit(limit)
    )
    stmt = _apply_style_filter(stmt, query.style)

    if cursor_product_added is not None and cursor_id is not None:
        stmt = stmt.where(
            tuple_(Brand.product_added, Brand.id)
            < tuple_(literal(cursor_product_added), literal(cursor_id))
        )

    rows = (await db.execute(stmt)).all()
    return [(r.Brand, int(r.product_count), int(r.new_count)) for r in rows]


async def _fetch_featured_phase_b(
    db: AsyncSession,
    query: BrandIndexQuery,
    *,
    phase_flag: bool,
    cursor_id: UUID | None,
    limit: int,
) -> list[tuple[Brand, int, int]]:
    agg_sq = _build_agg_subquery(_recent_cutoff())
    stmt = (
        select(
            Brand,
            func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
            func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
        )
        .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(
            Brand.is_active,
            Brand.is_featured == phase_flag,
            Brand.product_added.is_(None),
        )
        .order_by(Brand.id.desc())
        .limit(limit)
    )
    stmt = _apply_style_filter(stmt, query.style)

    if cursor_id is not None:
        stmt = stmt.where(Brand.id < cursor_id)

    rows = (await db.execute(stmt)).all()
    return [(r.Brand, int(r.product_count), int(r.new_count)) for r in rows]


# ---------- Shared helpers ----------


def _apply_style_filter(stmt, styles: list[str]):
    if not styles:
        return stmt
    # PostgreSQL array overlap (`&&`). Cast the array literal to `text[]`
    # explicitly so the operator resolves against the generic ARRAY column.
    # Without the cast, SQLAlchemy silently drops the predicate.
    return stmt.where(Brand.styles.op("&&")(cast(pg_array(styles), ARRAY(Text))))
