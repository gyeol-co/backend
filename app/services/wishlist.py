"""Wishlist service — state mutations.

스펙: app/backend/specs/discover.md #8-#9.

- add: advisory lock + 500 cap + dual-write (wishlist row + product_event row).
- remove: no advisory lock needed — single row DELETE, no cap check. dual-write only.
- 둘 다 idempotent: 중복 add / 없는 item delete 모두 200.

Transaction은 request-level `get_db_session` dep가 소유.
Count는 라우터에서 최종 응답 빌드할 때 조회.

list (`GET /wishlist`)은 routes/wishlist.py 핸들러에서 직접 처리.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppErrorCodeEnum, AppException
from app.models import Product, ProductEvent, ProductEventType, User, Wishlist

logger = logging.getLogger("gyeol.wishlist")

_WISHLIST_CAP = 500


async def add(db: AsyncSession, user: User, product_id: UUID) -> None:
    """Wishlist add — idempotent, cap-enforced. Duplicate call is a no-op.

    Raises `PRODUCT_NOT_FOUND` if the product is missing/inactive,
    `WISHLIST_FULL` when the user is already at the 500-item cap.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('wishlist:' || :uid::text))"),
        {"uid": str(user.id)},
    )

    product_ok = (
        await db.execute(select(Product.id).where(Product.id == product_id, Product.is_active))
    ).first()
    if product_ok is None:
        raise AppException(
            AppErrorCodeEnum.PRODUCT_NOT_FOUND,
            detail_payload={"product_id": str(product_id)},
        )

    existing = (
        await db.execute(
            select(Wishlist.product_id).where(
                Wishlist.user_id == user.id,
                Wishlist.product_id == product_id,
            )
        )
    ).first()
    if existing is not None:
        logger.info("wishlist.add.duplicate user_id=%s product_id=%s", user.id, product_id)
        return

    count = (
        await db.execute(select(func.count()).where(Wishlist.user_id == user.id))
    ).scalar_one()
    if count >= _WISHLIST_CAP:
        logger.info(
            "wishlist.add.full user_id=%s product_id=%s count=%d",
            user.id,
            product_id,
            count,
        )
        raise AppException(
            AppErrorCodeEnum.WISHLIST_FULL,
            detail_payload={"cap": _WISHLIST_CAP, "current": count},
        )

    # advisory lock + dup check guarantee no conflict; ON CONFLICT is belt + suspenders.
    await db.execute(
        text(
            "INSERT INTO wishlist (user_id, product_id) "
            "VALUES (:uid, :pid) ON CONFLICT DO NOTHING"
        ),
        {"uid": user.id, "pid": product_id},
    )
    await db.execute(
        insert(ProductEvent).values(
            user_id=user.id,
            type=ProductEventType.WISHLIST_ADD,
            product_id=product_id,
            properties={},
        )
    )
    logger.info("wishlist.add.ok user_id=%s product_id=%s", user.id, product_id)


async def remove(db: AsyncSession, user: User, product_id: UUID) -> None:
    """Wishlist remove — idempotent. No-existing row DELETE is a no-op."""
    deleted = (
        await db.execute(
            delete(Wishlist)
            .where(
                Wishlist.user_id == user.id,
                Wishlist.product_id == product_id,
            )
            .returning(Wishlist.product_id)
        )
    ).first()

    if deleted is None:
        logger.info("wishlist.remove.noop user_id=%s product_id=%s", user.id, product_id)
        return

    await db.execute(
        insert(ProductEvent).values(
            user_id=user.id,
            type=ProductEventType.WISHLIST_REMOVE,
            product_id=product_id,
            properties={},
        )
    )
    logger.info("wishlist.remove.ok user_id=%s product_id=%s", user.id, product_id)


__all__ = ["add", "remove"]
