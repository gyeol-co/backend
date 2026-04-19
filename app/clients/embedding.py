"""TEI embedding client — BAAI/bge-m3 (1024-dim).

Singleton `httpx.AsyncClient` with process lifetime. `close_client()` is called
from FastAPI lifespan. Fail-open contract: `embed_query` returns None on any
failure — callers degrade to FTS-only order. 2s timeout is strict; retry logic
lives outside this module (currently none — 0-users phase).
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger("gyeol.search.embedding")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=get_settings().EMBEDDING_URL,
            timeout=2.0,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def embed_query(q: str) -> list[float] | None:
    """Embed a single query string. Returns None on any failure.

    Fail-open: never raises. TEI `/embed` returns `[[...]]` for a single
    input (array-of-embeddings shape); this unwraps to the single embedding.
    """
    try:
        resp = await _get_client().post("/embed", json={"inputs": q})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data:
            logger.warning("search.embedding.bad_shape body_type=%s", type(data).__name__)
            return None
        first = data[0]
        if not isinstance(first, list):
            logger.warning("search.embedding.bad_inner_shape type=%s", type(first).__name__)
            return None
        return [float(x) for x in first]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        logger.warning("search.embedding.failed err=%s q_len=%d", e, len(q))
        return None
