from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _init() -> None:
    global _engine, _session_factory
    if _engine is not None:
        return
    settings = get_settings()
    _engine = create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        future=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def get_engine() -> AsyncEngine:
    _init()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    _init()
    assert _session_factory is not None
    return _session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """One transaction per request.

    Opens a session + `session.begin()` for the lifetime of the endpoint.
    On success the context manager commits; on any raised exception it
    rolls back. Services MUST NOT call `db.begin()` — they inherit this
    transaction.
    """
    factory = get_session_factory()
    async with factory() as session, session.begin():
        yield session
