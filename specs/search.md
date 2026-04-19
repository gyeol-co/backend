# Spec: search

> **spec-handshake 2026-04-19** — 키워드 기반 검색 (products + brands). Hybrid: FTS(GIN) candidate retrieval → TEI embedding cosine rerank → (optional, 주석 토글) TEI reranker rerank. Brands top-5 고정 + Products cursor pagination (sort별 dict key — brand/discover 규약). 로그인 불필요. 로그인 유저는 `search_event` 이력 저장. Suggestions 엔드포인트는 empty state 용 (response key: `recent`, `suggestions`).

디자인 기준: FE `/search` 화면 (디자인 파일 없음 — flow 추론). `Design context` 는 본 문서 맨 아래 Out of Scope 참고.
DB 현황: migration 001/007/008/009 — `product.search_vector` (GIN, GENERATED `to_tsvector('simple', search_text)`), `product.embedding vector(1024)` (HNSW cosine), `search_event` (hypertable, `user_id NOT NULL`). Brand 테이블엔 `search_vector` 없음 → runtime tsvector.

External services (`docker-compose`):
- TEI Embedding: `http://embedding:80/embed` — `BAAI/bge-m3`, output 1024-dim float.
- TEI Reranker: `http://reranker:80/rerank` — `BAAI/bge-reranker-v2-m3`, `(query, list[text])` → scored pairs.

## Summary

Gyeol 검색 기능 백엔드 스펙. 2개 라우트:

1. `GET /search` — keyword 기반 product + brand 검색. Products hybrid(FTS → embedding cosine → optional rerank), Brands runtime-tsvector full scan top-5. Cursor pagination은 products 만. 게스트 OK, 로그인 유저는 첫 페이지 valid 검색마다 `search_event` 1건 insert.
2. `GET /search/suggestions` — empty state 용. 로그인 유저: 최근 검색어 5 + 고정 suggestions 5. 비로그인: 고정 suggestions 5.

핵심 원칙:

- **Hybrid 단계적 degrade** — FTS → embedding → (optional) reranker. 각 단계 실패 시 fail-open: embedding 다운되면 FTS 순서 그대로, reranker 다운되면 embedding 순서 그대로. 5xx 응답 금지.
- **Reranker는 주석 토글** — 기본 OFF. `search_service.py` 상단 플래그 1개 + 블록 주석 처리로 on/off. 환경변수/feature flag **없음** (spec-driven, 재배포로 관리).
- **Brand 단순 처리** — ~100개 수준 full scan. runtime `to_tsvector('simple', coalesce(name,'')||' '||coalesce(name_ko,'')||' '||coalesce(description,''))`. 인덱스 없음. top-5 고정, pagination 없음.
- **FTS candidate top-200 고정** — embedding rerank 대상 size 한정(latency 제어). 나머지는 버린다 — FTS 매치가 200개 초과할 만큼 tail 검색은 이번 스펙에서 지원 안 함.
- **sort 선택 시 hybrid 무시** — `sort=recent|price_asc|price_desc`는 FTS candidate만 받고 embedding/reranker는 스킵. `sort=relevance`(기본)만 hybrid.
- **product search_vector는 GENERATED / simple 형태소** — `search_text` 컬럼에 staging ETL이 MeCab 토큰을 공백 구분으로 채워 넣음(migration 004 주석 참조). 쿼리도 `plainto_tsquery('simple', q)` 로 맞춘다.
- **Search state 없음** — 그냥 읽기 + append-only event. Dual-write 없음(상태 테이블 없으니).
- **External client fail-open + 2s timeout** — Embedding/Reranker 실패 시 5xx 금지, 단계 스킵 후 200.

## Scope

| # | Method | Path | 기능 |
|---|--------|------|------|
| 1 | `GET` | `/search` | Keyword → products + brands 검색 |
| 2 | `GET` | `/search/suggestions` | Empty state용 suggestions (최근 + 고정) |

---

## 1. `GET /search`

Hybrid keyword 검색. Products(cursor-paginated) + Brands(top-5 고정).

### Auth
선택. 비로그인 OK. 로그인 시 각 product 아이템에 `is_wishlisted`, 각 brand 아이템에 `is_followed` 채움. 로그인 유저는 첫 페이지 요청 시 `search_event` insert.

### Request

Query:

| param | 타입 | 필수 | 기본 | 설명 |
|-------|------|------|------|------|
| `q` | `str` (2~80) | Y | — | 검색어. `strip()` 적용 후 min length 2 체크 |
| `sort` | `Literal["relevance","recent","price_asc","price_desc"]` | N | `relevance` | `relevance` 만 embedding rerank 수행 |
| `category` | `ProductCategory \| None` | N | `None` | `outer/top/bottom/acc/shoes` — ENUM filter |
| `style` | `list[str]` | N | `[]` | wire: singular + repeat-encoded (`?style=minimal&style=tailored`). `product.styles && :style` (array overlap). 빈 배열 = 필터 없음 |
| `price_min` | `int \| None` (0~) | N | `None` | effective_price 하한(포함). `effective_price = coalesce(discounted_price, original_price)` |
| `price_max` | `int \| None` (0~) | N | `None` | effective_price 상한(포함) |
| `limit` | `int` (1~40) | N | `24` | products 페이지 크기 |
| `cursor_score` | `float \| None` | N | `None` | 직전 응답 `meta.next.cursor_score` 그대로 echo. `sort=relevance`에서만 유효 |
| `cursor_created` | `datetime \| None` (iso) | N | `None` | 직전 응답 `meta.next.cursor_created`. `sort=recent`에서만 유효 |
| `cursor_price` | `int \| None` | N | `None` | 직전 응답 `meta.next.cursor_price`. `sort=price_asc`/`sort=price_desc`에서만 유효 |
| `cursor_id` | `UUID \| None` | N | `None` | 모든 sort의 tie-breaker. cursor 키가 있으면 반드시 동반 |

Cursor wire format — discover/brand 규약과 동일. `meta.next`는 sort별 다른 key-value dict 로 노출되고, FE 는 각 키를 query string 으로 그대로 전개한다.

- `sort=relevance` 첫 페이지 → `cursor_score`/`cursor_id` 생략. keyset: `(combined_score DESC, id DESC)`.
- `sort=recent` 첫 페이지 → `cursor_created`/`cursor_id` 생략. keyset: `(created DESC, id DESC)`.
- `sort=price_asc` 첫 페이지 → `cursor_price`/`cursor_id` 생략. keyset: `(effective_price ASC NULLS LAST, id DESC)`.
- `sort=price_desc` 첫 페이지 → `cursor_price`/`cursor_id` 생략. keyset: `(effective_price DESC NULLS LAST, id DESC)`.

cursor 키와 `sort` 가 불일치 시 → 422 VALIDATION_ERROR. 예: `sort=recent` 인데 `cursor_score` 를 보내면 422.

`style` query param 은 **singular + repeat encoding** (brand 규약과 동일) — `?style=minimal&style=tailored` 로 전송, BE 에서는 `list[str]` 으로 수신한다. 브랜드 섹션은 cursor 없음(top-5 고정).

```python
# app/schemas/search.py

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.product import ProductCategory

SearchSort = Literal["relevance", "recent", "price_asc", "price_desc"]


class SearchQuery(BaseModel):
    """GET /search query parameters.

    `style` query param is singular + repeat-encoded: `?style=minimal&style=tailored`.
    Pydantic field name is `style` (list[str]). See brand/discover convention.

    Cursor fields are sort-scoped — validator enforces sort/cursor consistency
    and raises VALIDATION_ERROR on mismatch.
    """

    model_config = ConfigDict(extra="forbid")

    q: str = Field(max_length=80)  # strip 이후 min 2 는 validator 에서
    sort: SearchSort = "relevance"
    category: ProductCategory | None = None
    style: list[str] = []  # query param 'style' (singular), repeat-encoded
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    limit: int = Field(default=24, ge=1, le=40)

    # Sort-scoped cursor fields. Exactly one sort's keyset pair may be present.
    cursor_score: float | None = None          # sort=relevance
    cursor_created: datetime | None = None     # sort=recent
    cursor_price: int | None = None            # sort=price_asc | price_desc
    cursor_id: UUID | None = None              # all sorts, tie-breaker

    @field_validator("q", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> "SearchQuery":
        if len(self.q) < 2:
            # 짧은 쿼리는 422 VALIDATION_ERROR로 통일 (빈 쿼리도 strip 후 같은 경로).
            raise ValueError("q must be at least 2 characters after strip")
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("price_min must be <= price_max")

        # Sort ↔ cursor key 일관성. 각 sort 는 자신의 cursor 키만 허용.
        if self.sort == "relevance":
            if self.cursor_created is not None or self.cursor_price is not None:
                raise ValueError("only cursor_score/cursor_id valid for sort=relevance")
            if (self.cursor_score is None) != (self.cursor_id is None):
                raise ValueError("cursor_score and cursor_id must both be present or absent")
        elif self.sort == "recent":
            if self.cursor_score is not None or self.cursor_price is not None:
                raise ValueError("only cursor_created/cursor_id valid for sort=recent")
            if (self.cursor_created is None) != (self.cursor_id is None):
                raise ValueError("cursor_created and cursor_id must both be present or absent")
        else:  # price_asc | price_desc
            if self.cursor_score is not None or self.cursor_created is not None:
                raise ValueError("only cursor_price/cursor_id valid for sort=price_*")
            if (self.cursor_price is None) != (self.cursor_id is None):
                raise ValueError("cursor_price and cursor_id must both be present or absent")
        return self
```

### Response 200

```json
{
  "data": {
    "query": "wool trouser",
    "sort": "relevance",
    "products": {
      "items": [ /* ProductListItem[] (discover.py 재사용), rank/saved_at 은 null */ ],
      "total_candidates": 87
    },
    "brands": [
      {
        "id": "018f...",
        "slug": "studio-nicholson",
        "name": "STUDIO NICHOLSON",
        "name_ko": null,
        "logo_url": "https://cdn.gyeol.co/brands/018f.../logo.webp",
        "cover_url": "https://cdn.gyeol.co/brands/018f.../cover.webp",
        "platform": "musinsa",
        "styles": ["minimal", "tailored"],
        "is_featured": true,
        "is_followed": false,
        "product_count": 42,
        "new_count": 3,
        "product_added": "2026-04-16T10:00:00Z"
      }
    ],
    "meta_info": {
      "fts_applied": true,
      "embedding_applied": true,
      "reranker_applied": false,
      "degraded_reason": null
    }
  },
  "meta": {
    "request_id": "req_...",
    "next": {
      "sort": "relevance",
      "cursor_score": 0.872,
      "cursor_id": "018f-xxxxx"
    }
  }
}
```

- `products.items` — 최종 rerank 결과(하이브리드 최종 순서). 공통 `ProductListItem` (`app/schemas/discover.py`).
- `products.total_candidates` — FTS candidate 단계 매치 수(필터 적용 후, cursor 적용 전). `>200`이면 `200`으로 캡.
- `brands` — top-5 (또는 그 이하). 공통 `BrandIndexItem` (`app/schemas/brand.py`). `sort` 무관하게 항상 relevance 기반(brand rank 쿼리에서 ts_rank 사용).
- `meta_info` — debug/관찰용 필드. FE 표시 안 하지만 logs/개발 확인에 사용.
  - `fts_applied`: FTS candidate 쿼리 수행 여부 (항상 true)
  - `embedding_applied`: embedding rerank 수행 여부 (sort=relevance & TEI 성공 시 true)
  - `reranker_applied`: reranker 단계 수행 여부 (주석 off 상태면 항상 false)
  - `degraded_reason`: 단계 스킵 사유 (`null` | `"embedding_unavailable"` | `"sort_not_relevance"` | `"reranker_unavailable"`)

`meta.next` 형태 — sort별 key-value dict (brand/discover 규약과 동일). 더 이상 페이지 없으면 `meta.next: null`.

- `sort=relevance`: `{"sort":"relevance", "cursor_score": float, "cursor_id": uuid-str}`
- `sort=recent`: `{"sort":"recent", "cursor_created": iso-str, "cursor_id": uuid-str}`
- `sort=price_asc`: `{"sort":"price_asc", "cursor_price": int, "cursor_id": uuid-str}`
- `sort=price_desc`: `{"sort":"price_desc", "cursor_price": int, "cursor_id": uuid-str}`

Pagination 조건:

- 내부적으로 `limit+1` 뽑아 sentinel 존재하면 `meta.next` dict 제공, sentinel 없으면 `meta.next: null`.
- sentinel 제외한 items 의 마지막 row (`items[-1]`) 를 cursor anchor 로 사용.

### Validation Rules

- `q` strip 후 길이 < 2 → 422 `VALIDATION_ERROR` (detail `{"field":"q","msg":"too short"}`)
- `q` 길이 > 80 → 422
- `sort` enum 외 → 422
- `category` enum 외 → 422
- `limit` 범위 밖 → 422
- `price_min > price_max` → 422
- `cursor_*` 키가 `sort` 와 불일치 → 422 `VALIDATION_ERROR` (예: `sort=recent` 인데 `cursor_score` 동반)
- cursor keyset pair 불완전 → 422 (예: `cursor_score` 만 있고 `cursor_id` 누락)
- `cursor_id` UUID 파싱 실패 → 422
- `cursor_created` iso datetime 파싱 실패 → 422

### Business Logic

단계별. 각 단계 실패 원인 `degraded_reason` 으로 기록.

#### Step 1 — FTS candidate retrieval (top-K=200)

`plainto_tsquery('simple', :q)` 로 product `search_vector` 매치. sort=relevance 인 경우 `ts_rank_cd` 로 1차 relevance 정렬 후 K=200 slice. 다른 sort 는 해당 sort key 로 정렬.

- Filter 적용: `category`, `p.styles && :style` (DB column `styles`(plural) vs request param `style`(singular)), `effective_price` 범위, `is_active`.
- `is_soldout`은 필터 **안 함** (discover 와 동일 — FE 에서 label).
- cursor 있으면 cursor 조건 추가 (§Cursor logic).
- 결과 0개 → 즉시 200 반환 (empty items / total_candidates=0 / embedding_applied=false).

#### Step 2 — Embedding rerank (sort=relevance 한정)

`sort != "relevance"` → skip. `degraded_reason="sort_not_relevance"` (meta_info 에만 기록, 200 정상 응답).

`sort == "relevance"`:

1. Embedding client call: `EmbeddingClient.embed(query=q)` → `list[float]` (1024-dim)
   - `httpx.AsyncClient` timeout=2s. 실패 / timeout / 5xx → fail-open: candidates 를 FTS ts_rank_cd 순서 그대로 사용, `embedding_applied=false`, `degraded_reason="embedding_unavailable"`.
2. Candidates (Python memory) 의 `p.embedding` 이 `None`인 row 는 embedding-score 를 받지 못 하므로 FTS-only candidates 에 대해 다음 combined score 적용:
   - `combined_score = 0.3 * ts_rank_cd_normalized + 0.7 * cosine_sim`
   - `ts_rank_cd_normalized` = `ts_rank_cd / max_ts_rank` (0~1 정규화). max 가 0 이면 전부 0.
   - `cosine_sim` = `1 - cosine_distance(query_emb, p.embedding)` (임베딩 없는 row 는 0).
   - 가중치는 하드코드 (`WEIGHT_FTS=0.3`, `WEIGHT_EMBED=0.7`). feature flag 없음.
3. `combined_score DESC, id DESC` 재정렬. `meta.next.cursor_score` 는 이 combined_score.
4. cursor 가 요청에 있으면(`cursor_score`, `cursor_id` 모두 non-None) 재정렬 결과에서 `(combined_score, id) < (cursor_score, cursor_id)` row 만 유지 (row-constructor 비교와 동일 의미, app-layer 구현).

#### Step 3 — Reranker rerank (주석 토글 OFF 기본)

`search_service.py` 상단:

```python
# Reranker 토글 — 필요 시 블록 해제. 기본 OFF.
# ENABLE_RERANKER = True
ENABLE_RERANKER = False
```

`ENABLE_RERANKER == True` 인 경우 Step 2 결과 상위 N=30 을 `RerankerClient.rerank(query=q, texts=[...])` 로 재정렬(나머지는 original 순서 뒤에 append). Client 실패 → fail-open: Step 2 결과 그대로, `reranker_applied=false`, `degraded_reason="reranker_unavailable"`. Reranker 에 보낼 `text` 는 `p.name + " " + (p.description or "")` 최대 512 chars.

#### Step 4 — Pagination slice

- `limit+1` 기준 slice. last row sentinel 이 있으면 sentinel 을 버리고 items 의 **마지막 실제 아이템**(= items[limit-1]) 기준으로 `meta.next` dict 구성.
- `meta.next` dict (sort 별):
  - relevance: `{"sort": "relevance", "cursor_score": combined_score, "cursor_id": str(id)}`
  - recent: `{"sort": "recent", "cursor_created": created.isoformat(), "cursor_id": str(id)}`
  - price_asc|desc: `{"sort": <sort>, "cursor_price": effective_price, "cursor_id": str(id)}`
- sentinel 없으면 `meta.next = None`.

#### Step 5 — Enrich (is_wishlisted, is_followed)

- `fetch_wishlisted_ids` / `fetch_followed_brand_ids` (기존 helper 재사용). 비로그인 전부 false.
- Brand 섹션 top-5 — 아래 §Brand section 참조.

#### Step 6 — Search event (로그인 & 첫 페이지만)

- **첫 페이지 판정**: 모든 cursor 필드(`cursor_score`, `cursor_created`, `cursor_price`, `cursor_id`)가 `None` 인 경우. `user is not None` 이고 valid 쿼리여야 함.
- `search_event` (hypertable): `{user_id, query, result_count, created}`. `result_count` = `products.items.len` (현재 페이지).
- dual-write 없음. 같은 transaction 내에서 INSERT (get_db_session 트랜잭션 포함). 실패 시 `logger.warning` 후 swallow 하고 응답 진행 — event 는 검색 결과 제공의 blocker 가 되면 안 된다.
- 후속 페이지(cursor_* 중 하나라도 있음) 에는 event 기록 안 함 — 같은 세션 중복 기록 방지.

### DB Queries

#### Step 1 — FTS candidates (sort=relevance)

```sql
-- :q  = 원문 (strip 후)
-- :category (ENUM | NULL)
-- :style   (text[], 빈 배열이면 필터 skip) — wire param name 은 singular
-- :price_min, :price_max
WITH q_ts AS (SELECT plainto_tsquery('simple', :q) AS tsq)
SELECT
  p.id, p.name, p.description,
  p.category, p.subcategory, p.sub_category_detail,
  p.fit, p.color, p.pattern, p.material, p.details,
  p.styles, p.tags,
  p.thumbnail_key, p.image_keys, p.url,
  p.original_price, p.discounted_price, p.discount_rate, p.is_soldout,
  p.is_active, p.created, p.popularity,
  p.embedding,
  ts_rank_cd(p.search_vector, q_ts.tsq) AS ts_rank,
  coalesce(p.discounted_price, p.original_price) AS effective_price,
  b.id AS brand_id, b.slug AS brand_slug, b.name AS brand_name,
  b.name_ko AS brand_name_ko, b.logo_key, b.platform
FROM product p
CROSS JOIN q_ts
JOIN brand b ON b.id = p.brand_id AND b.is_active
WHERE p.is_active
  AND p.search_vector @@ q_ts.tsq
  AND (:category IS NULL OR p.category = :category)
  AND (cardinality(:style) = 0 OR p.styles && :style)
  AND (:price_min IS NULL OR coalesce(p.discounted_price, p.original_price) >= :price_min)
  AND (:price_max IS NULL OR coalesce(p.discounted_price, p.original_price) <= :price_max)
ORDER BY ts_rank DESC, p.id DESC
LIMIT 200;
```

- `idx_product_search` (gin) 로 `@@` 매치, `is_active` partial 필터. `b.is_active` 필터는 `WHERE` 에 — brand 미활성 상품 은 결과에서 제외.
- `cardinality(:style) = 0` 로 빈 배열 safe bypass.
- `total_candidates` = 이 쿼리가 반환한 row 수 (LIMIT 200 에 걸리면 200).
- **Cursor 적용**: `sort=relevance` 의 cursor anchor 는 **combined_score**(Step 2 에서 계산) 이므로, Step 1 의 SQL 자체에는 cursor 필터를 넣지 않는다. `cursor_score`/`cursor_id` 가 있는 요청이라도 Step 1 은 top-200 candidate 를 그대로 재계산 — embedding rerank 가 후에 combined_score 를 매기고 application-layer 에서 `(combined_score, id) < (cursor_score, cursor_id)` 로 필터 + slice. (= 매 요청마다 200 candidate 재평가. K=200 이므로 허용.)

#### Step 1 — FTS candidates (sort=recent, cursor=created/id)

```sql
WITH q_ts AS (SELECT plainto_tsquery('simple', :q) AS tsq)
SELECT /* 동일 컬럼셋, ts_rank/embedding 포함 X */
FROM product p
CROSS JOIN q_ts
JOIN brand b ON b.id = p.brand_id AND b.is_active
WHERE p.is_active
  AND p.search_vector @@ q_ts.tsq
  AND (...filters...)
  AND (:cursor_created IS NULL
       OR (p.created, p.id) < (:cursor_created, :cursor_id))
ORDER BY p.created DESC, p.id DESC
LIMIT :limit + 1;
```

sort=recent 는 200 cap 불필요 — embedding rerank 없음. cursor 로 바로 page slice.

#### Step 1 — FTS candidates (sort=price_asc | price_desc)

```sql
WITH q_ts AS (...)
SELECT ..., coalesce(p.discounted_price, p.original_price) AS effective_price
FROM product p
CROSS JOIN q_ts
JOIN brand b ON b.id = p.brand_id AND b.is_active
WHERE p.is_active
  AND p.search_vector @@ q_ts.tsq
  AND (...)
  AND (:cursor_price IS NULL
       OR (coalesce(p.discounted_price, p.original_price), p.id) <OP> (:cursor_price, :cursor_id))
ORDER BY effective_price <DIR> NULLS LAST, p.id DESC
LIMIT :limit + 1;
```

- `<DIR>` = `ASC` 또는 `DESC`.
- `<OP>` = sort=price_asc 이면 `>`, sort=price_desc 이면 `<`.
- NULLS LAST 처리는 price column 이 `original_price`(NOT NULL) 기반 coalesce 라 실제 null 없음. 방어적으로 NULLS LAST 명시.

#### Step 2 — Embedding rerank (application-layer)

DB 쿼리 없음. Python 에서:

```python
# query_emb: list[float] from EmbeddingClient
# candidates: list[Row] with p.embedding nullable
from numpy import dot, linalg

def _cosine(a: list[float], b: list[float]) -> float:
    # zero-guard: if embedding missing, return 0
    if not a or not b:
        return 0.0
    na = linalg.norm(a)
    nb = linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(dot(a, b) / (na * nb))

max_ts = max((c.ts_rank for c in candidates), default=0.0)
for c in candidates:
    ts_norm = (c.ts_rank / max_ts) if max_ts > 0 else 0.0
    cos_sim = _cosine(query_emb, c.embedding) if c.embedding else 0.0
    c.score = WEIGHT_FTS * ts_norm + WEIGHT_EMBED * cos_sim
candidates.sort(key=lambda c: (-c.score, str(c.id).lower()), reverse=False)
```

`numpy` 이미 `pgvector` 종속 — 추가 install 불필요.

#### Brand section — runtime tsvector top-5

```sql
WITH q_ts AS (SELECT plainto_tsquery('simple', :q) AS tsq)
SELECT
  b.id, b.slug, b.name, b.name_ko, b.logo_key, b.cover_key, b.platform,
  b.styles, b.is_featured, b.product_added,
  cnt.product_count, cnt.new_count,
  ts_rank(
    to_tsvector(
      'simple',
      coalesce(b.name, '') || ' ' ||
      coalesce(b.name_ko, '') || ' ' ||
      coalesce(b.description, '')
    ),
    q_ts.tsq
  ) AS brand_rank
FROM brand b
CROSS JOIN q_ts
LEFT JOIN LATERAL (
  SELECT
    count(*) FILTER (WHERE p.is_active) AS product_count,
    count(*) FILTER (WHERE p.is_active AND p.created >= :recent_cutoff) AS new_count
  FROM product p
  WHERE p.brand_id = b.id
) cnt ON true
WHERE b.is_active
  AND to_tsvector(
        'simple',
        coalesce(b.name, '') || ' ' ||
        coalesce(b.name_ko, '') || ' ' ||
        coalesce(b.description, '')
      ) @@ q_ts.tsq
ORDER BY brand_rank DESC, b.id DESC
LIMIT 5;
```

- 인덱스 없음 — seq scan. 브랜드 ~100개 스케일에선 OK. 수천개 넘어가면 `ALTER TABLE brand ADD COLUMN search_vector tsvector GENERATED ...` migration 추가 고려(이번 스콥 out).
- embedding 없음 — brand 에는 `embedding` 컬럼 없음. FTS relevance 그대로 반환.
- `recent_cutoff = now() - interval '7 days'` (discover/brand 와 동일 규약).
- sort 와 무관하게 항상 이 쿼리 — brand 섹션은 keyword 관련성만.

#### Search event insert (로그인 + 첫 페이지)

```sql
INSERT INTO search_event (user_id, query, result_count)
VALUES (:user_id, :q, :result_count);
```

`id`, `created` 는 default. 실패 시 swallow (transaction 영향 최소화는 현재 구조 상 어려움 — 같은 tx 이지만 route 의 `get_db_session` 이 commit 하므로 rollback 가능. 실패 시 route 에서 try/except 로 감싸 응답 블로킹 방지).

### Indexes 활용

- `idx_product_search` (gin on `search_vector`) — migration 001. `@@` 매치 가속.
- `idx_product_embedding` (hnsw cosine) — 사용 안 함. cosine_sim 은 candidates in-memory 로 계산(candidates 최대 200 건, 쿼리당 임베딩 1024 * 200 * 8B = 1.6MB — 수용 가능).
- `idx_product_category`, `idx_product_styles`, `idx_product_brand` — filter composite.
- `idx_search_user` — user 별 recent 검색어 lookup (suggestions).
- Brand 검색 — 인덱스 없음 (`CREATE INDEX` 추가 금지 — 미래 검토).

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | q 길이, sort/category enum, price range, cursor 불일치 |

Embedding/Reranker 실패는 **에러 아님** — fail-open, 200 응답에 `meta_info.degraded_reason` 으로 통지.

### Side Effects

- 로그인 유저 + 첫 페이지(cursor 필드 전체 None) + valid 쿼리: `search_event` 1 row INSERT.
- INSERT 실패 시 logger.warning, 무시.
- 후속 페이지, 비로그인: 이벤트 없음.

---

## 2. `GET /search/suggestions`

Empty state (검색 페이지 진입 직후 input 비어있는 상태) 용.

### Auth
선택.

### Request

Query: 없음.

### Response 200

로그인 유저:

```json
{
  "data": {
    "recent": ["wool trouser", "knit vest", "loafer"],
    "suggestions": ["minimal", "archive", "tailored", "knit", "leather"]
  },
  "meta": { "request_id": "req_..." }
}
```

비로그인:

```json
{
  "data": {
    "recent": [],
    "suggestions": ["minimal", "archive", "tailored", "knit", "leather"]
  },
  "meta": { "request_id": "req_..." }
}
```

- `recent` — 유저의 최근 검색어 distinct 최대 5개. 최신순. 없으면 빈 배열.
- `suggestions` — 하드코드 5개 (상수). 내부 constant 변수명은 `FIXED_SUGGESTIONS`, 응답 JSON key는 `suggestions`. `app/constants/search.py`.

### Validation
없음.

### Data Access

```sql
-- 로그인 유저 전용. 최근 30일 내, distinct, 최신순 5.
SELECT query
FROM (
  SELECT query, max(created) AS last_used
  FROM search_event
  WHERE user_id = :user_id
    AND created >= now() - interval '30 days'
  GROUP BY query
  ORDER BY last_used DESC
  LIMIT 5
) t
ORDER BY last_used DESC;
```

`idx_search_user (user_id, created DESC)` 활용. 30일 윈도우는 compression policy(7일) 이후 chunk 에 여전히 닿지만 compressed read OK (timescale).

### Indexes
- `idx_search_user` — user 필터 prefix.

### Error Cases
없음 (비로그인도 200).

### Side Effects
없음.

---

## Shared Concerns

### Constants — Fixed suggestions

```python
# app/constants/search.py

from __future__ import annotations

# Hardcoded suggestions list. Variable name intentionally stays `FIXED_SUGGESTIONS`
# (internal semantic), but the response JSON key is `suggestions` — see
# SuggestionsResponse. Service serializes as:
#     SuggestionsResponse(recent=..., suggestions=FIXED_SUGGESTIONS)
FIXED_SUGGESTIONS: list[str] = [
    "minimal",
    "archive",
    "tailored",
    "knit",
    "leather",
]

# FTS candidate top-K (embedding rerank 대상 size 상한).
FTS_CANDIDATE_LIMIT: int = 200

# Embedding rerank 가중치. sum 은 1.0 이 자연스러움.
WEIGHT_FTS: float = 0.3
WEIGHT_EMBED: float = 0.7

# Reranker 대상 size (Step 3에서 상위 N 만 reranker 입력).
RERANKER_TOP_N: int = 30

# Max query length (Pydantic 에도 동기).
MAX_QUERY_LEN: int = 80

# Recent search history window
RECENT_SEARCH_WINDOW_DAYS: int = 30
RECENT_SEARCH_LIMIT: int = 5
```

### External Clients — `app/clients/`

신규 디렉토리. `httpx.AsyncClient` singleton(process-lifetime). `lifespan` 에 close.

```
app/clients/
├── __init__.py
├── embedding.py    — EmbeddingClient (TEI /embed)
└── reranker.py     — RerankerClient (TEI /rerank)
```

#### `app/clients/embedding.py`

```python
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger("gyeol.search.embedding")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=get_settings().EMBEDDING_URL,
            timeout=2.0,  # strict — fail-open on timeout
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def embed_query(q: str) -> list[float] | None:
    """Embed a single query string. Returns None on any failure.

    Fail-open contract: never raise. Callers proceed without embedding rerank
    when this returns None.
    """
    try:
        resp = await _get_client().post("/embed", json={"inputs": q})
        resp.raise_for_status()
        data: list[list[float]] = resp.json()
        if not data or not isinstance(data, list):
            logger.warning("search.embedding.bad_shape body=%s", data)
            return None
        return data[0]  # TEI returns [[...]] for single input
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning("search.embedding.failed err=%s q_len=%d", e, len(q))
        return None
```

#### `app/clients/reranker.py`

```python
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
    """Score `texts` against `query`. Returns per-text score list or None on failure.

    TEI response shape: `[{"index": int, "score": float}, ...]` in descending score.
    We reassemble into index-aligned scores (so callers can re-sort their original
    candidate list by these scores).
    """
    try:
        resp = await _get_client().post(
            "/rerank",
            json={"query": query, "texts": texts, "raw_scores": False},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.warning("search.reranker.bad_shape body=%s", data)
            return None
        scores = [0.0] * len(texts)
        for item in data:
            scores[int(item["index"])] = float(item["score"])
        return scores
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        logger.warning("search.reranker.failed err=%s n=%d", e, len(texts))
        return None
```

`app/main.py` lifespan 에 close 추가:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await close_redis()
    await embedding_client.close_client()  # 신규
    await reranker_client.close_client()  # 신규
```

### Config 추가 — `app/core/config.py`

```python
EMBEDDING_URL: str = "http://embedding:80"
RERANKER_URL: str = "http://reranker:80"
```

### Pydantic DTOs

```python
# app/schemas/search.py

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.product import ProductCategory
from app.schemas.brand import BrandIndexItem
from app.schemas.discover import ProductListItem

SearchSort = Literal["relevance", "recent", "price_asc", "price_desc"]


class SearchQuery(BaseModel):
    """GET /search query parameters.

    `q` is trimmed (strip) before length validation. Final length must be >= 2.
    `style` is singular + repeat-encoded at the wire (`?style=a&style=b`).
    Cursor fields are sort-scoped — validator enforces sort/cursor consistency.
    """

    model_config = ConfigDict(extra="forbid")

    q: str = Field(max_length=80)
    sort: SearchSort = "relevance"
    category: ProductCategory | None = None
    style: list[str] = []  # wire: ?style=a&style=b (singular)
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    limit: int = Field(default=24, ge=1, le=40)

    cursor_score: float | None = None          # sort=relevance
    cursor_created: datetime | None = None     # sort=recent
    cursor_price: int | None = None            # sort=price_asc | price_desc
    cursor_id: UUID | None = None              # all sorts, tie-breaker

    @field_validator("q", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check(self) -> "SearchQuery":
        if len(self.q) < 2:
            raise ValueError("q must be >= 2 chars after strip")
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("price_min must be <= price_max")
        if self.sort == "relevance":
            if self.cursor_created is not None or self.cursor_price is not None:
                raise ValueError("only cursor_score/cursor_id valid for sort=relevance")
            if (self.cursor_score is None) != (self.cursor_id is None):
                raise ValueError("cursor_score and cursor_id must both be present or absent")
        elif self.sort == "recent":
            if self.cursor_score is not None or self.cursor_price is not None:
                raise ValueError("only cursor_created/cursor_id valid for sort=recent")
            if (self.cursor_created is None) != (self.cursor_id is None):
                raise ValueError("cursor_created and cursor_id must both be present or absent")
        else:  # price_asc | price_desc
            if self.cursor_score is not None or self.cursor_created is not None:
                raise ValueError("only cursor_price/cursor_id valid for sort=price_*")
            if (self.cursor_price is None) != (self.cursor_id is None):
                raise ValueError("cursor_price and cursor_id must both be present or absent")
        return self


class SearchProductsSection(BaseModel):
    items: list[ProductListItem]
    total_candidates: int


class SearchMetaInfo(BaseModel):
    fts_applied: bool
    embedding_applied: bool
    reranker_applied: bool
    degraded_reason: Literal[
        "embedding_unavailable",
        "sort_not_relevance",
        "reranker_unavailable",
    ] | None = None


class SearchResponse(BaseModel):
    query: str
    sort: SearchSort
    products: SearchProductsSection
    brands: list[BrandIndexItem]
    meta_info: SearchMetaInfo


class SuggestionsResponse(BaseModel):
    """Response shape for GET /search/suggestions.

    Wire JSON keys: `recent`, `suggestions` (not `fixed`).
    """

    recent: list[str]
    suggestions: list[str]
```

### Route 구조

```
app/api/routes/
├── auth.py         (기존)
├── brand.py        (기존)
├── discover.py     (기존)
├── wishlist.py     (기존)
└── search.py       (신규) — GET /search, GET /search/suggestions
```

`main.py` 에 `app.include_router(search_routes.router)` 추가.

```python
# app/api/routes/search.py

router = APIRouter(prefix="/search", tags=["search"])
```

### Service 구조

```
app/services/
└── search.py       (신규) — hybrid orchestration
```

이유 — hybrid pipeline 이 4 단계(FTS → embedding → reranker → slice) 며 주석 토글 / fail-open 분기가 route 하드코드로 담기엔 커짐. Service 로 분리.

```python
# app/services/search.py

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

# from app.clients import reranker as reranker_client  # Reranker 토글 시 주석 해제
from app.clients import embedding as embedding_client
from app.constants.search import (
    FTS_CANDIDATE_LIMIT,
    MAX_QUERY_LEN,
    RERANKER_TOP_N,
    WEIGHT_EMBED,
    WEIGHT_FTS,
)

# Reranker 토글 — 기본 OFF. 필요 시 True 로 변경 + 위 import 주석 해제.
ENABLE_RERANKER: bool = False

logger = logging.getLogger("gyeol.search")


@dataclass
class SearchOutput:
    products: list["ProductListItem"]  # type: ignore[name-defined]
    total_candidates: int
    # sort별 key-value dict. None 이면 다음 페이지 없음. Shape:
    #   relevance: {"sort":"relevance", "cursor_score": float, "cursor_id": str}
    #   recent:    {"sort":"recent",    "cursor_created": iso-str, "cursor_id": str}
    #   price_*:   {"sort": <sort>,     "cursor_price": int, "cursor_id": str}
    next_cursor: dict[str, Any] | None
    fts_applied: bool
    embedding_applied: bool
    reranker_applied: bool
    degraded_reason: str | None


async def run_products_search(
    db: AsyncSession,
    user: User | None,
    query: SearchQuery,
) -> SearchOutput: ...


async def run_brands_search(
    db: AsyncSession,
    user: User | None,
    q: str,
) -> list[BrandIndexItem]: ...
```

route 에서는 두 service 호출 을 `asyncio.gather` 로 병렬.

### Logging

- Logger names: `gyeol.search`, `gyeol.search.embedding`, `gyeol.search.reranker`.
- 이벤트:
  - `search.fts.ok q_len=.. candidates=..`
  - `search.embedding.applied candidates=..`
  - `search.embedding.failed err=..` (fail-open)
  - `search.reranker.applied n=..`
  - `search.reranker.failed err=..`
  - `search.degraded reason=..`
  - `search.event.stored user_id=.. q_len=..`
  - `search.event.failed err=..`
- 에러 stack → `logger.exception`.

### Transaction Boundaries

| 엔드포인트 | Transaction | 이유 |
|-----------|-------------|------|
| `GET /search` | single tx (get_db_session) | 읽기 쿼리 + search_event INSERT 동일 tx. INSERT 실패 시 try/except 로 감싸 응답 분리(INSERT 실패가 read 결과 버리지 않도록 savepoint 사용 or event INSERT 마지막 스텝). 구현 단순화를 위해 **event INSERT 를 응답 생성 후 별도 `db.commit()` 시점**에 합침 — route 의 dep 가 transaction commit 담당이니 service 내부 INSERT 후 예외는 logger.warning 후 `db.rollback()` 대신 **swallow 없이 에러 전파는 금지**. 구체적으로: service 내부에서 event INSERT 전용 `try/except AppException: raise` → 그 외 `except Exception: await db.rollback(); 새 tx 시작 안 함, 응답 그대로 계속` 는 **불가** (session begin 이미 consume). 결론: event INSERT 는 응답에 blocking. 실패 시 500 반환 허용 — 실제로 TimescaleDB INSERT 실패는 드물기에 감수. (향후 `commit+begin_nested` 패턴으로 분리 가능) |
| `GET /search/suggestions` | single tx (read-only) | — |

**결정**: event INSERT 실패는 500 으로 내보낸다. TimescaleDB 가용성 이슈로 INSERT 가 실패하면 검색 결과도 못 준다는 의미 — 실제론 DB down 이니 합당. 만약 향후 user 경험 우선순위가 올라가면 event INSERT 를 `commit_after_response` 패턴으로 분리.

> 현재 discover spec (`product.view_event`) 은 `fire-and-forget` 으로 service 내부에서 `db.commit()` + 자체 예외 swallow 를 하고 있으나, 이 경우 `get_db_session` 이 할당한 transaction 이 service 내부에서 commit 되어 session 이 'fresh' 상태가 된다. search 에서도 같은 패턴을 쓸 수 있으나, **search 의 메인 응답 데이터는 read-only 이므로 event 가 마지막 스텝** — 그냥 trailing INSERT 로 두고 dep 의 commit 에 맡긴다. 이 전략이 discover view-event 보다 단순하고 신뢰성 높다. 실제 commit 실패는 500 으로 관찰.

### External Clients — Resilience 요약

| Client | Base URL | Timeout | Retry | On failure |
|--------|----------|---------|-------|-----------|
| EmbeddingClient | `EMBEDDING_URL` (default `http://embedding:80`) | 2s | 없음 | fail-open: `degraded_reason="embedding_unavailable"`, FTS 순서 유지 |
| RerankerClient | `RERANKER_URL` (default `http://reranker:80`) | 2s | 없음 | fail-open: `degraded_reason="reranker_unavailable"`, Step 2 순서 유지 |

- Retry 없음 — p95 latency 예산(~500ms) 내에서 한 번만 시도. TEI 웜업 중이면 2s 내 실패 → 그 페이지 는 degraded 응답, 유저 retry 시 자연 회복.
- Circuit breaker 없음 — 0 users 단계에서 과잉.

### Error Enum (AppErrorCodeEnum)

본 스펙에서 사용하는 코드 — 전부 **이미 `app/core/errors.py`에 정의되어 있음**:

| Code | HTTP | 기존 여부 |
|------|------|-----------|
| `VALIDATION_ERROR` | 422 | 기존 |
| `INTERNAL_ERROR` | 500 | 기존 (event INSERT 실패 희귀 케이스) |

**신규 enum 추가 없음.** `SEARCH_QUERY_TOO_SHORT`, `EMBEDDING_UNAVAILABLE` 둘 다 **도입하지 않는다**:
- `SEARCH_QUERY_TOO_SHORT` → `VALIDATION_ERROR` + `detail_payload={"field":"q","msg":"too_short"}` 로 통합. auth 스펙 선례와 동일 패턴.
- `EMBEDDING_UNAVAILABLE` → 에러 코드 아님. 200 + `meta_info.degraded_reason="embedding_unavailable"`.

### Performance Expectations

| Phase | Latency 예산 |
|-------|-------------|
| FTS candidate (K=200) | < 50ms (GIN scan + tsvector match) |
| Embedding HTTP call | < 150ms (TEI cpu or gpu, 웜 상태) |
| In-memory cosine (200 vec × 1024 dim) | < 10ms (numpy) |
| (optional) Reranker HTTP call (N=30) | < 200ms |
| Brand FTS (~100 rows seq scan) | < 20ms |
| Enrich (wishlist/follow batch) | < 10ms |
| Total p95 | ~300ms (reranker off), ~500ms (reranker on) |

Cold start 이슈:
- TEI embedding/reranker 컨테이너는 첫 요청 시 모델 로드 수 초 소요. 운영은 `docker-compose` startup 후 dummy `/embed?q=init` 호출로 웜업(out-of-scope, ops 담당).
- BE 관점: cold start 상태에서 2s timeout → fail-open → 200 (degraded). 유저는 빈-검색 대신 FTS-only 결과 수신.

---

## Schema Alignment

`data/migrations/001_init.up.sql` + `004_search_text` + `007_discover_support` + `008_public_product_refine` + `009_public_brand_platform` 기준.

| 스펙 사용 | 스키마 | 비고 |
|-----------|--------|------|
| `product.search_vector` (tsvector, GENERATED) + `idx_product_search` (gin) | ✓ | 001 |
| `product.search_text` (staging ETL 채움) | ✓ | 004 |
| `product.embedding` (vector(1024)) + `idx_product_embedding` (hnsw cosine) | ✓ | 001. 이번 스펙은 in-memory cosine 사용(hnsw 쿼리 안 함) |
| `product.popularity`, filter 컬럼 (category, styles, fit, price) | ✓ | 007 + 008 |
| `brand.{name, name_ko, description}` runtime tsvector | ✓ | 001. 인덱스 없음 (full scan) |
| `search_event` (hypertable, user_id NOT NULL) + `idx_search_user (user_id, created DESC)` | ✓ | 001 |
| `wishlist`, `brand_follow` (is_wishlisted / is_followed enrich) | ✓ | 001 |

**DB schema gaps: 0건.** 마이그레이션 신규 없음.

**ORM 모델 gap: 1건.**

- `SearchEvent` ORM 모델 **없음** — `app/models/search_event.py` 신규 작성 필요. `ProductEvent` 와 동일 패턴 (hypertable, `id insert_default=uuid7_std`, `user_id NOT NULL`, ENUM 없이 `query: text`, `result_count: int`, `created` `insert=False`).

```python
# app/models/search_event.py (신규)

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DeclarativeRoot, uuid7_std


class SearchEvent(DeclarativeRoot):
    """Append-only search query events (TimescaleDB hypertable).

    `user_id` is NOT NULL — guest events are not inserted. Schema has no real
    PK (hypertable partitions on `created`); ORM declares `id` as primary_key
    only to satisfy the mapper.
    """

    __tablename__ = "search_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        init=False,
        primary_key=True,
        insert_default=uuid7_std,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        init=False,
        server_default=func.now(),
        nullable=False,
    )
```

`app/models/__init__.py` 에 `SearchEvent` export 추가.

---

## Out of Scope

1. **Autocomplete / as-you-type suggestions** — 현재 suggestions 는 empty state 전용. typing 중 API 호출 없음.
2. **Brand search_vector 컬럼 / GIN 인덱스** — 브랜드 ~100개 스케일에서 full scan OK. 수천 넘으면 재검토.
3. **Reranker default ON** — 주석 off 기본. 전면 롤아웃 시 ENABLE_RERANKER=True + 성능 측정.
4. **Circuit breaker / retry / hedged request** — 0 users 단계 과잉.
5. **Personalized search rerank** (user embedding mixing) — data pipeline affinity 시그널 활용은 추후 feature.
6. **Search history UI (delete, clear)** — `DELETE /search/history` 같은 mutation 없음. suggestions read-only.
7. **Filter facets (카테고리별 카운트, style 태그 매트릭스)** — brand products 의 `count_by_category` 같은 facet 응답 없음.
8. **Price range histogram / stepped filter UI 지원** — FE 는 min/max 입력만.
9. **Multi-locale search** — `simple` config 고정. 영문 stemming(`english` config) 분기 없음.
10. **Trending / popular queries** — fixed suggestions 는 하드코드. 집계 기반 trending 차기.
11. **Spell correction / "did you mean"** — TEI reranker 의 subsemantic matching 로 일부 커버.
12. **Rate limiting / ETag / 304** — auth 라인 정리 후 공통 미들웨어로 추후.
13. **A/B WEIGHT_FTS / WEIGHT_EMBED 실험** — 하드코드. feature flag 없음.
14. **외부 브랜드 / 외부 product URL 결과 병합** — 전부 자사 DB 만.
15. **Image-based search** — text-only.

---

## Decision Log

| # | Decision | Why |
|---|----------|-----|
| 1 | FTS → embedding → reranker 단계적 degrade, 각 단계 fail-open | 외부 서비스 의존성 하에서 5xx 최소화. 검색 결과 없음보다 FTS-only 결과가 나음. |
| 2 | Reranker 는 기본 OFF (코드 주석 토글) | 초기 품질 관찰 단계. 비용/latency 트레이드오프 측정 전 까진 보수적 운용. |
| 3 | Embedding cosine 을 in-memory 로 계산(HNSW 쿼리 안 함) | candidates K=200 수준에서 numpy in-memory 가 latency 낮고 코드 단순. HNSW 는 query-to-product vector 검색이 아니라 item-to-item 검색에 적합. |
| 4 | Brand 섹션은 runtime tsvector + seq scan | 스케일 작음. 인덱스 추가는 schema 변경 비용. |
| 5 | sort=recent / price_* 는 embedding/reranker 스킵 | 사용자 의도가 keyword relevance 가 아니라 시간/가격 정렬. combined score 재정렬은 역효과. |
| 6 | `combined_score = 0.3 * ts_rank_norm + 0.7 * cosine` 하드코드 | feature flag 없음. 경험치 초기값. 이후 A/B 거쳐 조정. |
| 7 | 검색 이력은 로그인 + 첫 페이지 만 | 같은 세션 중복 기록 방지. cursor 있는 요청 은 pagination 단계. |
| 8 | cursor 는 sort별 dict key (brand/discover 규약) — `meta.next = {"sort":..., "cursor_<key>":..., "cursor_id":...}`, 각 키를 query string 으로 그대로 전개 | spec-handshake 합의: 프로젝트 내 cursor 규약 통일. base64 opaque 방식 폐기 (FE 라우팅도 dict 가 더 단순). |
| 9 | `SEARCH_QUERY_TOO_SHORT` / `EMBEDDING_UNAVAILABLE` enum 추가 없음 | q 짧음은 VALIDATION_ERROR + detail_payload. embedding 다운은 에러 아니라 200 + degraded_reason. |
| 10 | FTS candidate top-K=200 고정 | embedding rerank latency 예산 내에서 수용 가능한 상한. K 늘리면 cosine 연산량 ∝ K. |
| 11 | Embedding/Reranker 는 `app/clients/` 디렉토리 신설 (singleton httpx) | 향후 다른 external service(추천, 이미지 분석) 붙일 때 자연 확장. |
| 12 | `product.search_text` + `plainto_tsquery('simple', q)` | staging ETL 이 MeCab 토큰을 공백 구분으로 채운 형태 맞춤 (migration 004 주석). config `simple` 고정. |
| 13 | Brand pagination 없음, top-5 고정 | FE 디자인 (추정) 에서 주 결과가 products. brands 는 보조 섹션. |
| 14 | `meta_info` 필드는 debug/관찰용으로 응답에 포함 | FE 는 표시 안 하지만 grafana/log 분석에 유용. 별도 header 쓰기보단 envelope 내부. |
| 15 | `search_event` INSERT 는 blocking (실패 시 500 허용) | 구현 단순화. 향후 `commit_after_response` 분리 검토. discover view-event 의 self-commit 패턴 복제 안 함 (trailing INSERT 로 충분). |

---

## Checklist

- [x] 2 routes (`/search`, `/search/suggestions`)
- [x] Auth / Request / Response / Validation / Business logic / DB queries / Indexes / Errors / Side effects 각 섹션
- [x] Hybrid pipeline 단계별 (FTS candidate → embedding cosine → optional reranker → slice → enrich → event)
- [x] Cursor wire format (sort별 dict key — `meta.next = {"sort", "cursor_<key>", "cursor_id"}`, brand/discover 규약)
- [x] External clients (EmbeddingClient, RerankerClient) 인터페이스 + fail-open 정책
- [x] Pydantic DTOs (SearchQuery, SearchProductsSection, SearchMetaInfo, SearchResponse, SuggestionsResponse)
- [x] Constants 모듈 (`FIXED_SUGGESTIONS`, FTS_CANDIDATE_LIMIT, WEIGHT_*, RERANKER_TOP_N, query length)
- [x] Route / service 구조
- [x] Schema alignment — DB 0 gap, ORM 1 gap (`SearchEvent` 모델 신규)
- [x] Transaction boundaries 명시
- [x] Performance expectations (p95 ~300ms / ~500ms)
- [x] Error enum — 신규 0건
- [x] Out of Scope 15건
- [x] Decision Log 15건
