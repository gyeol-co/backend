"""Brand follow service — state mutations.

스펙: app/backend/specs/brand.md §4-§5.

- follow: slug→brand 검증 + dual-write (brand_follow row + brand_event row).
- unfollow: slug→brand 검증 + dual-write (DELETE + event).
- 둘 다 idempotent: 중복 follow / 없는 follow 제거 모두 200.
- Follow cap **없음** → advisory lock 불필요 (ON CONFLICT DO NOTHING + RETURNING
  으로 no-op 판정).

Transaction은 request-level `get_db_session` dep가 소유.
follower_count는 라우터에서 응답 빌드할 때 조회.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppErrorCodeEnum, AppException
from app.models import Brand, BrandEvent, BrandEventType, BrandFollow, User

logger = logging.getLogger("gyeol.brand")


async def follow(db: AsyncSession, user: User, slug: str) -> UUID:
    """Brand follow — idempotent. Returns `brand_id`.

    Raises `BRAND_NOT_FOUND` if the slug is missing/inactive.
    """
    brand_id = await _resolve_active_brand_id(db, slug)

    inserted = (
        await db.execute(
            text(
                "INSERT INTO brand_follow (user_id, brand_id) "
                "VALUES (:uid, :bid) "
                "ON CONFLICT (user_id, brand_id) DO NOTHING "
                "RETURNING brand_id"
            ),
            {"uid": user.id, "bid": brand_id},
        )
    ).first()

    if inserted is not None:
        await db.execute(
            insert(BrandEvent).values(
                user_id=user.id,
                type=BrandEventType.FOLLOW,
                brand_id=brand_id,
                properties={},
            )
        )
        logger.info("brand.follow.ok user_id=%s brand_id=%s slug=%s", user.id, brand_id, slug)
    else:
        logger.info(
            "brand.follow.duplicate user_id=%s brand_id=%s slug=%s", user.id, brand_id, slug
        )
    return brand_id


async def unfollow(db: AsyncSession, user: User, slug: str) -> UUID:
    """Brand unfollow — idempotent. No-existing follow DELETE → no-op.

    slug lookup은 여전히 수행 — 없으면 `BRAND_NOT_FOUND` (spec decision #10).
    Returns `brand_id`.
    """
    brand_id = await _resolve_active_brand_id(db, slug)

    deleted = (
        await db.execute(
            delete(BrandFollow)
            .where(
                BrandFollow.user_id == user.id,
                BrandFollow.brand_id == brand_id,
            )
            .returning(BrandFollow.brand_id)
        )
    ).first()

    if deleted is not None:
        await db.execute(
            insert(BrandEvent).values(
                user_id=user.id,
                type=BrandEventType.UNFOLLOW,
                brand_id=brand_id,
                properties={},
            )
        )
        logger.info("brand.unfollow.ok user_id=%s brand_id=%s slug=%s", user.id, brand_id, slug)
    else:
        logger.info(
            "brand.unfollow.noop user_id=%s brand_id=%s slug=%s", user.id, brand_id, slug
        )
    return brand_id


async def _resolve_active_brand_id(db: AsyncSession, slug: str) -> UUID:
    brand_id = (
        await db.execute(select(Brand.id).where(Brand.slug == slug, Brand.is_active))
    ).scalar_one_or_none()
    if brand_id is None:
        raise AppException(
            AppErrorCodeEnum.BRAND_NOT_FOUND,
            detail_payload={"slug": slug},
        )
    return brand_id


__all__ = ["follow", "unfollow"]
