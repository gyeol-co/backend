from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from app.core.config import get_settings

_pool: ConnectionPool | None = None


def get_redis_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(
            get_settings().REDIS_URL,
            decode_responses=True,
            max_connections=64,
        )
    return _pool


async def get_redis() -> Redis:
    """FastAPI dependency — returns a Redis client bound to the shared pool."""
    return Redis(connection_pool=get_redis_pool())


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
