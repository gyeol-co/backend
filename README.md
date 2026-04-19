# gyeol-backend

> Fashion curation platform API — FastAPI + SQLAlchemy 2.0 async + asyncpg.
> Part of the [Gyeol](https://gyeol.co) project (편집숍의 안목 + 플랫폼의 기술).

## Stack

| Layer | Tech |
|-------|------|
| Web framework | FastAPI 0.115+ (async routes, CORS / request-id middleware) |
| ORM | SQLAlchemy 2.0 async, `MappedAsDataclass`, asyncpg driver |
| Database | PostgreSQL 18 + pgvector + TimescaleDB hypertables |
| Cache / sessions | Redis 7 |
| Storage | AWS S3 (public read for product thumbnails) |
| Auth | Email + password (argon2) / OAuth (Kakao · Naver · Google) / signed session cookie via itsdangerous |
| Search | Hybrid — Postgres FTS (tsvector / GIN) + pgvector embeddings (HNSW) + TEI `bge-m3` + optional cross-encoder reranker |
| Korean NLP | `python-mecab-ko` for symmetric query ↔ document tokenisation, hand-curated synonym dictionary |
| Package / runtime | `uv`, Python 3.12 |

## Structure

```
app/
├── api/
│   ├── deps.py                   # DbSession, CurrentUser, OptionalUser dependencies
│   └── routes/                   # thin HTTP handlers — one router per feature
│       ├── auth.py               # signup / login / OAuth callback / session cookie
│       ├── discover.py           # popular / new / foryou / following / detail / related
│       ├── brand.py              # index / detail / products / follow · unfollow
│       ├── wishlist.py           # add / remove / list (Wishlist M:N + event dual-write)
│       ├── search.py             # hybrid search + suggestions
│       ├── _brand_mapping.py     # ORM → BrandIndexItem DTO mappers
│       └── _product_mapping.py   # ORM → ProductListItem DTO mappers
├── clients/
│   ├── embedding.py              # TEI /embed — bge-m3 (1024-dim, fail-open)
│   └── reranker.py               # TEI /rerank — bge-reranker-v2-m3 (fail-open)
├── core/
│   ├── config.py                 # pydantic-settings env loader
│   ├── db.py                     # one transaction per request via `get_db_session`
│   ├── envelope.py               # StandardSuccessResponse / StandardErrorResponse
│   ├── errors.py                 # AppException + AppErrorCodeEnum (central HTTP mapping)
│   ├── redis.py, s3.py, session_cookie.py, request_id.py
│   └── password.py
├── models/                       # SQLAlchemy 2.0 Mapped classes — taxonomy-driven
│   ├── brand.py, product.py, user.py, oauth.py, consent_log.py
│   ├── wishlist.py, brand_follow.py, user_product_affinity.py
│   └── brand_event.py, product_event.py, search_event.py   # hypertables
├── oauth_providers/              # one strategy per provider (authorize / exchange / userinfo)
│   ├── base.py, kakao.py, naver.py, google.py
├── schemas/                      # Pydantic v2 DTOs — request queries + response envelopes
│   └── auth.py, brand.py, discover.py, search.py
├── services/                     # business logic — only when routes grow complex
│   ├── auth.py                   # signup race handling, OAuth pending-token handshake
│   ├── brand_follow.py           # dual-write (brand_follow + brand_event)
│   ├── wishlist.py               # wishlist cap + advisory lock
│   ├── product.py                # detail + view-event fire-and-forget
│   ├── search.py                 # hybrid pipeline orchestration
│   ├── _tokenizer.py             # MeCab POS filter + stop-words
│   ├── _tsquery.py               # tsquery builder with synonym OR-expansion
│   └── _synonyms.py              # yaml loader for synonyms.yaml
├── constants/
│   └── search.py                 # FTS candidate cap, rerank weights, etc.
└── main.py                       # FastAPI app factory + global exception handlers
```

## Request flow

```
client → FastAPI route   (DbSession dep opens tx)
           │
           ├── route handler (thin — parse input, delegate)
           │     └── service (business logic, raises AppException)
           │           └── ORM / external client (TEI, S3)
           │
           └── response wrapped in StandardSuccessResponse
                 └── get_db_session commits (or rollback on exception)
```

Errors always flow through `AppException(AppErrorCodeEnum.X)` so the global handler emits the standard envelope:

```jsonc
{
  "data": null,
  "meta": {
    "request_id": "req_...",
    "error": { "code": "VALIDATION_ERROR", "detail_payload": {...} }
  }
}
```

## Search pipeline (highlights)

```
query
  ├─ MeCab tokenise + synonym expand → tsquery `(자켓 | 재킷 | jacket) & ...`
  ├─ FTS candidate (GIN index, top-K=200)
  │     └─ empty? → embedding-only ANN fallback (HNSW) + TEI reranker
  ├─ embedding rerank (TEI /embed → cosine, combined with ts_rank)
  ├─ category/subcategory intent boost (`셔츠` → subcategory=shirt +2.0)
  ├─ cursor-filter + slice
  └─ enrich (is_wishlisted / is_followed) + search_event log (login, first page)
```

Every pipeline stage fails open — TEI down just means ts_rank-only ordering, never a 5xx.

## Local run

```bash
cp .env.example .env       # fill SESSION_SECRET + OAuth + AWS keys
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

Postgres + Redis + TEI run from the sibling `deploy/` compose.

## Portfolio note

This backend deliberately **doesn't hide complexity behind clever abstractions**:

- one transaction per HTTP request (no nested `db.begin()` in services)
- handlers stay thin — business logic lives in explicit service modules when it outgrows a route
- every failure mode is typed (`AppErrorCodeEnum`), never bare `HTTPException`
- external calls (TEI, S3) are fail-open by default to protect the happy path

It's a 0-users MVP sized for one person to operate, with clear seams for each piece to scale later (multi-replica BE, dedicated reranker service, separate search cluster, etc.).
