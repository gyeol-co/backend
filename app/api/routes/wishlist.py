"""Wishlist routes — list / add / remove.

List (`GET /wishlist`)은 SQLAlchemy select()로 핸들러 안에서 직접 처리.
Mutation (POST/DELETE)만 services.wishlist에 위임 — advisory lock + dual-write.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.api.routes._product_mapping import to_list_item
from app.core.envelope import StandardResponseMeta, StandardSuccessResponse
from app.core.request_id import current_request_id
from app.models import Brand, Product, ProductCategory, Wishlist
from app.schemas.discover import (
    ProductListItem,
    WishlistListQuery,
    WishlistListResponse,
    WishlistToggleResponse,
)
from app.services import wishlist as wishlist_service

router = APIRouter(prefix="/wishlist", tags=["wishlist"])


# ---------- GET /wishlist ----------


@router.get(
    "",
    response_model=StandardSuccessResponse[WishlistListResponse],
    name="wishlist.list",
)
async def _(
    query: Annotated[WishlistListQuery, Depends()],
    db: DbSession,
    user: CurrentUser,
) -> StandardSuccessResponse[WishlistListResponse]:
    items_stmt = (
        select(Product, Brand, Wishlist.created.label("saved_at"))
        .join(Product, Product.id == Wishlist.product_id)
        .join(Brand, Brand.id == Product.brand_id)
        .where(Wishlist.user_id == user.id, Product.is_active)
    )
    if query.category is not None:
        items_stmt = items_stmt.where(Product.category == query.category)
    items_stmt = (
        items_stmt.order_by(Wishlist.created.desc(), Wishlist.product_id.desc())
        .offset(query.skip)
        .limit(query.limit + 1)
    )

    count_total_stmt = select(func.count()).select_from(Wishlist).where(Wishlist.user_id == user.id)
    count_by_cat_stmt = (
        select(Product.category, func.count().label("cnt"))
        .join(Wishlist, Wishlist.product_id == Product.id)
        .where(Wishlist.user_id == user.id, Product.is_active)
        .group_by(Product.category)
    )

    items_result, total_result, by_cat_result = await asyncio.gather(
        db.execute(items_stmt),
        db.execute(count_total_stmt),
        db.execute(count_by_cat_stmt),
    )

    rows = items_result.all()
    has_next = len(rows) > query.limit
    page_rows = rows[: query.limit]

    count_total = int(total_result.scalar_one() or 0)

    # 빈 카테고리 0 채움 — 5개 key 항상 포함.
    count_by_category: dict[str, int] = {cat.value: 0 for cat in ProductCategory}
    for cat, cnt in by_cat_result.all():
        count_by_category[cat.value] = int(cnt)

    items: list[ProductListItem] = [
        to_list_item(p, b, is_wishlisted=True, saved_at=saved_at) for p, b, saved_at in page_rows
    ]

    next_params: dict[str, Any] | None = None
    if has_next:
        next_params = {"skip": query.skip + query.limit, "limit": query.limit}
        if query.category is not None:
            next_params["category"] = query.category.value

    return StandardSuccessResponse(
        data=WishlistListResponse(
            count_total=count_total,
            count_by_category=count_by_category,
            items=items,
        ),
        meta=StandardResponseMeta(request_id=current_request_id(), next=next_params),
    )


# ---------- POST /wishlist/{product_id} ----------


@router.post(
    "/{product_id}",
    response_model=StandardSuccessResponse[WishlistToggleResponse],
    name="wishlist.add",
)
async def _(
    product_id: UUID,
    db: DbSession,
    user: CurrentUser,
) -> StandardSuccessResponse[WishlistToggleResponse]:
    await wishlist_service.add(db, user, product_id)
    count = (
        await db.execute(select(func.count()).where(Wishlist.user_id == user.id))
    ).scalar_one()
    return StandardSuccessResponse(
        data=WishlistToggleResponse(
            product_id=product_id, is_wishlisted=True, wishlist_count=count
        ),
        meta=StandardResponseMeta(request_id=current_request_id()),
    )


# ---------- DELETE /wishlist/{product_id} ----------


@router.delete(
    "/{product_id}",
    response_model=StandardSuccessResponse[WishlistToggleResponse],
    name="wishlist.remove",
)
async def _(
    product_id: UUID,
    db: DbSession,
    user: CurrentUser,
) -> StandardSuccessResponse[WishlistToggleResponse]:
    await wishlist_service.remove(db, user, product_id)
    count = (
        await db.execute(select(func.count()).where(Wishlist.user_id == user.id))
    ).scalar_one()
    return StandardSuccessResponse(
        data=WishlistToggleResponse(
            product_id=product_id, is_wishlisted=False, wishlist_count=count
        ),
        meta=StandardResponseMeta(request_id=current_request_id()),
    )
