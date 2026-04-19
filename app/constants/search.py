"""Search feature constants.

스펙: app/backend/specs/search.md §Shared Concerns §Constants.
"""

from __future__ import annotations

# Hardcoded suggestions list. Variable name stays `FIXED_SUGGESTIONS` (internal
# semantic); response JSON key is `suggestions` (see SuggestionsResponse). Service
# serializes as: SuggestionsResponse(recent=..., suggestions=FIXED_SUGGESTIONS).
FIXED_SUGGESTIONS: list[str] = [
    "minimal",
    "archive",
    "tailored",
    "knit",
    "leather",
]

# FTS candidate top-K cap (embedding rerank 대상 size 상한).
FTS_CANDIDATE_LIMIT: int = 200

# Embedding rerank 가중치. sum ≈ 1.0.
WEIGHT_FTS: float = 0.3
WEIGHT_EMBED: float = 0.7

# Fallback 전용 reranker 설정 — cross-encoder (TEI bge-reranker-v2-m3) 가
# embedding-only fallback 결과의 상위 N 건을 재정렬. FTS 히트 경로에선 사용 안 함.
RERANKER_TOP_N: int = 30
RERANKER_TEXT_MAX: int = 512

# Max query length — Pydantic 과 동기.
MAX_QUERY_LEN: int = 80

# Recent search history window (suggestions 엔드포인트).
RECENT_SEARCH_WINDOW_DAYS: int = 30
RECENT_SEARCH_LIMIT: int = 5

# Brand 섹션 top-K (pagination 없음).
BRAND_TOP_K: int = 5

# Brand 집계의 "new" 기준 (discover/brand 규약 — 최근 7일).
BRAND_RECENT_CUTOFF_DAYS: int = 7
