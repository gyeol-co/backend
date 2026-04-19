"""Product service — detail endpoint with view-event side effect.

Simple read queries (feed/related/wishlist list) are implemented inline in
route handlers. This service exists for `get_detail` because it has a
fire-and-forget event INSERT that commits its own transaction.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes._product_mapping import to_product_detail
from app.core.errors import AppErrorCodeEnum, AppException
from app.models import (
    Brand,
    BrandFollow,
    Product,
    ProductEvent,
    ProductEventType,
    User,
    Wishlist,
)
from app.schemas.discover import ProductDetail, ProductStats

logger = logging.getLogger("gyeol.product")


async def get_detail(
    db: AsyncSession,
    user: User | None,
    product_id: UUID,
) -> ProductDetail:
    """상품 상세 + stats. 로그인 시 view event fire-and-forget.

    Parallelizes: is_wishlisted / is_followed / view_count_7d / wishlist_count.
    View event INSERT commits its own txn so failures can't poison the session.
    """
    row = (
        await db.execute(
            select(Product, Brand)
            .join(Brand, Brand.id == Product.brand_id)
            .where(Product.id == product_id, Product.is_active)
        )
    ).one_or_none()
    if row is None:
        raise AppException(
            AppErrorCodeEnum.PRODUCT_NOT_FOUND,
            detail_payload={"product_id": str(product_id)},
        )
    product, brand = row

    view_cutoff = datetime.now(UTC) - timedelta(days=7)

    view_stmt = select(func.count()).where(
        ProductEvent.product_id == product.id,
        ProductEvent.type == ProductEventType.VIEW,
        ProductEvent.created >= view_cutoff,
    )
    wish_count_stmt = select(func.count()).where(Wishlist.product_id == product.id)

    if user is not None:
        wl_stmt = select(func.count()).where(
            Wishlist.user_id == user.id,
            Wishlist.product_id == product.id,
        )
        bf_stmt = select(func.count()).where(
            BrandFollow.user_id == user.id,
            BrandFollow.brand_id == brand.id,
        )
        wl_res, bf_res, view_res, wish_res = await asyncio.gather(
            db.execute(wl_stmt),
            db.execute(bf_stmt),
            db.execute(view_stmt),
            db.execute(wish_count_stmt),
        )
        is_wishlisted = wl_res.scalar_one() > 0
        is_followed = bf_res.scalar_one() > 0
    else:
        view_res, wish_res = await asyncio.gather(
            db.execute(view_stmt),
            db.execute(wish_count_stmt),
        )
        is_wishlisted = False
        is_followed = False

    stats = ProductStats(
        view_count_7d=int(view_res.scalar_one() or 0),
        wishlist_count=int(wish_res.scalar_one() or 0),
    )

    # Fire-and-forget view event (로그인 시만). Commit own txn, swallow errors.
    if user is not None:
        await _log_view_event(db, user.id, product.id)

    logger.info(
        "product.detail.ok product_id=%s user=%s",
        product.id,
        user.id if user else None,
    )
    return to_product_detail(
        product,
        brand,
        is_wishlisted=is_wishlisted,
        is_followed=is_followed,
        stats=stats,
    )


async def _log_view_event(
    db: AsyncSession,
    user_id: UUID,
    product_id: UUID,
) -> None:
    """Insert 'view' product_event and commit. Never raises."""
    try:
        await db.execute(
            insert(ProductEvent).values(
                user_id=user_id,
                type=ProductEventType.VIEW,
                product_id=product_id,
                properties={"source": "detail"},
            )
        )
        await db.commit()
    except Exception as e:  # noqa: BLE001 — swallow all
        logger.warning(
            "product.view_event.failed user_id=%s product_id=%s err=%s",
            user_id,
            product_id,
            e,
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass


__all__ = ["get_detail"]
