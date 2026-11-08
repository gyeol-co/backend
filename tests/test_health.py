from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app import main


@pytest.mark.parametrize("failed_dependency", [None, "postgres", "redis"])
async def test_readiness_checks_dependencies_without_failing_liveness(
    monkeypatch, failed_dependency
):
    connection = Mock(execute=AsyncMock())
    redis = Mock(ping=AsyncMock())
    if failed_dependency == "postgres":
        connection.execute.side_effect = OSError("database unavailable")
    if failed_dependency == "redis":
        redis.ping.side_effect = OSError("redis unavailable")

    @asynccontextmanager
    async def connect():
        yield connection

    monkeypatch.setattr(main, "get_engine", lambda: Mock(connect=connect))
    monkeypatch.setattr(main, "get_redis", AsyncMock(return_value=redis))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.create_app()), base_url="http://test"
    ) as client:
        ready = await client.get("/readyz")
        live = await client.get("/healthz")

    assert ready.status_code == (503 if failed_dependency else 200)
    assert ready.json() == {"status": "unavailable" if failed_dependency else "ok"}
    assert live.status_code == 200


def test_synonyms_are_available_from_the_backend_repository():
    from app.services import _synonyms

    assert _synonyms._resolve_synonyms_path().is_file()
    assert "재킷" in _synonyms.expand_token("자켓")
