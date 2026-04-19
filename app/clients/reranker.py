"""TEI reranker client — BAAI/bge-reranker-v2-m3.

Used ONLY on the embedding-only fallback path (FTS returned 0). FTS-hit
candidates already have the keyword signal, so the cross-encoder reranker
earns its latency budget only when embeddings are the sole ordering signal.
Singleton `httpx.AsyncClient`; fail-open — `None` on any failure.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger("gyeol.search.reranker")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=get_settings().RERANKER_URL,
            timeout=2.0,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def rerank(query: str, texts: list[str]) -> list[float] | None:
    """Score `texts` against `query`. Returns per-text scores index-aligned to
    the input `texts`, or None on failure (caller falls back to the caller's
    existing order).

    TEI response shape: `[{"index": int, "score": float}, ...]` (sorted by
    score desc). Reassembled into an index-aligned list so callers can sort
    their original candidate list by these scores.
    """
    if not texts:
        return []
    try:
        resp = await _get_client().post(
            "/rerank",
            json={"query": query, "texts": texts, "raw_scores": False},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.warning("search.reranker.bad_shape body_type=%s", type(data).__name__)
            return None
        scores = [0.0] * len(texts)
        for item in data:
            idx = int(item["index"])
            if 0 <= idx < len(texts):
                scores[idx] = float(item["score"])
        return scores
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as e:
        logger.warning("search.reranker.failed err=%s n=%d", e, len(texts))
        return None
