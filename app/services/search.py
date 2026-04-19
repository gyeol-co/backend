"""Search service — hybrid orchestration (FTS → embedding cosine rerank,
with embedding-only fallback + cross-encoder reranker when FTS is empty).

스펙: app/backend/specs/search.md §Business Logic.

Pipeline:
  Step 1  FTS candidate retrieval (GIN tsvector match, MeCab + synonym
          expansion on the query side).
              - sort=relevance: top-K=200, order by ts_rank_cd DESC.
              - sort=recent / price_*: cursor-applied page query.
  Step 2  Embedding rerank (sort=relevance, FTS returned ≥1).
              - TEI /embed → cosine sim → combined_score = 0.3*ts_norm + 0.7*cos.
              - Fail-open on TEI error: ts_rank-only order, degraded_reason set.
  Step 3  Embedding-only fallback + cross-encoder reranker
          (sort=relevance, FTS returned 0).
              - HNSW ANN over product.embedding with the same filters.
              - TEI /rerank on top-N of the ANN result to tighten ordering.
              - Fail-open: if reranker unavailable, keep ANN cosine order.
  Step 4  Pagination slice (limit+1 sentinel → next cursor dict).
  Step 5  Enrich (is_wishlisted / is_followed).
  Step 6  Search event (login + first page → fire-and-forget insert).

Brand section (`run_brands_search`) is ILIKE substring match on
name / name_ko / description (~100 rows, full scan).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import (
    ARRAY,
    Text,
    and_,
    case,
    cast,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes._brand_mapping import fetch_followed_brand_ids, to_brand_index_item
from app.api.routes._product_mapping import fetch_wishlisted_ids, to_list_item
from app.clients import embedding as embedding_client
from app.clients import reranker as reranker_client
from app.constants.search import (
    BRAND_RECENT_CUTOFF_DAYS,
    BRAND_TOP_K,
    FTS_CANDIDATE_LIMIT,
    RECENT_SEARCH_LIMIT,
    RECENT_SEARCH_WINDOW_DAYS,
    RERANKER_TEXT_MAX,
    RERANKER_TOP_N,
    WEIGHT_EMBED,
    WEIGHT_FTS,
)
from app.models import Brand, Product, SearchEvent, User
from app.models.product import ProductCategory
from app.schemas.brand import BrandIndexItem
from app.schemas.discover import ProductListItem
from app.schemas.search import SearchDegradedReason, SearchMetaInfo, SearchQuery
from app.services._synonyms import expand_token
from app.services._tokenizer import tokenize_query
from app.services._tsquery import build_tsquery_string

logger = logging.getLogger("gyeol.search")


# ---- Category / subcategory intent detection ----
#
# Users type "셔츠" or "재킷" expecting to see shirts / jackets at the top —
# but FTS + embedding alone rank by thematic similarity (e.g. the query
# "하와이안 셔츠" pulls tropical-printed hoodies above plain shirts). We
# address this post-hoc: detect category/subcategory intent from the query
# tokens and add a score bonus to matching products.
#
# Mappings are hard-coded against the product taxonomy (data/common/
# taxonomy.yaml). Keep in sync when new subcategory values land.

_SUBCATEGORY_TO_CATEGORY: dict[str, ProductCategory] = {
    # top
    "tee": ProductCategory.TOP,
    "shirt": ProductCategory.TOP,
    "sweatshirt": ProductCategory.TOP,
    "knit": ProductCategory.TOP,
    "cardigan": ProductCategory.TOP,
    "hoodie": ProductCategory.TOP,
    "polo": ProductCategory.TOP,
    "blouse": ProductCategory.TOP,
    # outer
    "jacket": ProductCategory.OUTER,
    "blouson": ProductCategory.OUTER,
    "coat": ProductCategory.OUTER,
    "blazer": ProductCategory.OUTER,
    "vest": ProductCategory.OUTER,
    "down": ProductCategory.OUTER,
    "fleece": ProductCategory.OUTER,
    "parka": ProductCategory.OUTER,
    "anorak": ProductCategory.OUTER,
    # bottom
    "pants": ProductCategory.BOTTOM,
    "jeans": ProductCategory.BOTTOM,
    "slacks": ProductCategory.BOTTOM,
    "shorts": ProductCategory.BOTTOM,
    "skirt": ProductCategory.BOTTOM,
    # acc
    "hat": ProductCategory.ACC,
    "cap": ProductCategory.ACC,
    "belt": ProductCategory.ACC,
    "bag": ProductCategory.ACC,
    "backpack": ProductCategory.ACC,
    "tote": ProductCategory.ACC,
    "scarf": ProductCategory.ACC,
    "jewelry": ProductCategory.ACC,
    "socks": ProductCategory.ACC,
    "wallet": ProductCategory.ACC,
    "gloves": ProductCategory.ACC,
    "glasses": ProductCategory.ACC,
    "sunglasses": ProductCategory.ACC,
    # shoes
    "sandal": ProductCategory.SHOES,
    "sneaker": ProductCategory.SHOES,
    "loafer": ProductCategory.SHOES,
    "boot": ProductCategory.SHOES,
    "derby": ProductCategory.SHOES,
    "oxford": ProductCategory.SHOES,
}

# Keywords that only indicate category (no specific subcategory). Used when a
# query token expands to a generic category term but not a concrete subcategory.
_CATEGORY_KEYWORDS: dict[str, ProductCategory] = {
    "top": ProductCategory.TOP,
    "tops": ProductCategory.TOP,
    "상의": ProductCategory.TOP,
    "탑": ProductCategory.TOP,
    "outer": ProductCategory.OUTER,
    "outerwear": ProductCategory.OUTER,
    "아우터": ProductCategory.OUTER,
    "외투": ProductCategory.OUTER,
    "bottom": ProductCategory.BOTTOM,
    "bottoms": ProductCategory.BOTTOM,
    "하의": ProductCategory.BOTTOM,
    "보텀": ProductCategory.BOTTOM,
    "shoes": ProductCategory.SHOES,
    "shoe": ProductCategory.SHOES,
    "신발": ProductCategory.SHOES,
    "슈즈": ProductCategory.SHOES,
    "accessory": ProductCategory.ACC,
    "accessories": ProductCategory.ACC,
    "acc": ProductCategory.ACC,
    "액세서리": ProductCategory.ACC,
}

# Score bonuses. Non-overlapping: a subcategory match implies a category match,
# but we apply the strongest single tier (subcategory > category > none) so
# subcategory hits strictly outrank category-only hits.
_SUBCATEGORY_BOOST: float = 2.0
_CATEGORY_BOOST: float = 1.0


def _detect_category_intent(
    tokens: list[str],
) -> tuple[ProductCategory | None, str | None]:
    """Scan query tokens (incl. synonym expansions) for a subcategory or
    category intent. Subcategory wins over category-only keyword (stricter
    signal). `(None, None)` when no signal found.
    """
    variants = {v.lower() for t in tokens for v in expand_token(t)}
    for v in variants:
        if v in _SUBCATEGORY_TO_CATEGORY:
            return _SUBCATEGORY_TO_CATEGORY[v], v
    for v in variants:
        if v in _CATEGORY_KEYWORDS:
            return _CATEGORY_KEYWORDS[v], None
    return None, None


# ---- Public API ----


@dataclass
class ProductsSearchResult:
    items: list[ProductListItem]
    total_candidates: int
    next_cursor: dict[str, Any] | None
    meta_info: SearchMetaInfo
    result_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.result_count = len(self.items)


async def run_products_search(
    db: AsyncSession,
    user: User | None,
    query: SearchQuery,
) -> ProductsSearchResult:
    """Products hybrid search. Branches by sort, enriches, returns page + cursor."""
    # Query-side tokenisation mirrors the ETL (`data/etl/gold/tokenize.py`) so
    # the document-side search_vector and the query-side tsquery share the same
    # lexemes. On top of that we expand each token with its synonym group
    # (`data/common/synonyms.yaml`) so users typing `자켓` also hit `재킷` /
    # `jacket` — MeCab does not merge those on its own.
    tokens = tokenize_query(query.q)
    tsq_string = build_tsquery_string(tokens)
    # Category/subcategory intent — used as a score bonus in `_rerank_and_slice`.
    target_category, target_subcategory = _detect_category_intent(tokens)

    # Postgres `to_tsquery(regconfig, text)` — the `simple` config must be cast
    # to `regconfig` explicitly; a plain varchar bind parameter does NOT resolve
    # to the regconfig overload. `plainto_tsquery` is kept as the fallback when
    # tokenisation collapses to an empty string so we still attempt a match
    # against the raw query instead of 0-ing out.
    regconfig_simple = cast(literal("simple"), REGCONFIG)
    if tsq_string:
        tsq = func.to_tsquery(regconfig_simple, literal(tsq_string))
    else:
        tsq = func.plainto_tsquery(regconfig_simple, literal(query.q.strip()))
    search_vector = literal_column("product.search_vector")
    effective_price = func.coalesce(Product.discounted_price, Product.original_price)

    # FTS predicates (used for the FTS-first path).
    fts_where = [
        Product.is_active,
        Brand.is_active,
        search_vector.op("@@")(tsq),
    ]
    # Filters (category / style / price) apply to both FTS path and the
    # embedding-only fallback.
    filter_where = [Product.is_active, Brand.is_active]
    if query.category is not None:
        filter_where.append(Product.category == query.category)
    if query.style:
        filter_where.append(
            Product.styles.op("&&")(cast(pg_array(query.style), ARRAY(Text)))
        )
    if query.price_min is not None:
        filter_where.append(effective_price >= query.price_min)
    if query.price_max is not None:
        filter_where.append(effective_price <= query.price_max)

    where_clauses = fts_where + filter_where[2:]  # skip duplicate is_active

    # Total-match count for `total_candidates` (relevance caps at K=200).
    count_stmt = (
        select(func.count())
        .select_from(Product)
        .join(Brand, Brand.id == Product.brand_id)
        .where(*where_clauses)
    )

    if query.sort == "relevance":
        candidates, total_match = await _run_relevance(
            db, query, tsq, search_vector, effective_price, where_clauses, count_stmt
        )
        total_candidates = min(total_match, FTS_CANDIDATE_LIMIT)

        # FTS-empty → embedding-only ANN fallback (keeps same filters).
        # Single embed call shared between rerank + fallback to avoid 2× TEI hops.
        query_emb = await embedding_client.embed_query(query.q)
        fallback_used = False
        if not candidates and query_emb is not None:
            candidates = await _run_embedding_fallback(
                db, query, query_emb, effective_price, filter_where
            )
            total_candidates = len(candidates)
            if candidates:
                fallback_used = True
                # Fallback-only cross-encoder rerank — FTS-hit candidates already
                # carry keyword signal, so reranker budget is spent only where
                # ordering relies purely on embeddings.
                await _apply_fallback_reranker(query.q, candidates)
                logger.info(
                    "search.fallback.embedding q_len=%d candidates=%d",
                    len(query.q),
                    len(candidates),
                )

        page_candidates, next_cursor, meta_info = _rerank_and_slice(
            query,
            candidates,
            query_emb,
            prescored=fallback_used,
            target_category=target_category,
            target_subcategory=target_subcategory,
        )
        page_rows = [(c.product, c.brand) for c in page_candidates]
    else:
        page_plus_sentinel, total_match = await _run_sorted(
            db, query, tsq, search_vector, effective_price, where_clauses, count_stmt
        )
        total_candidates = total_match
        has_next = len(page_plus_sentinel) > query.limit
        page_rows = page_plus_sentinel[: query.limit]
        next_cursor = _build_next_cursor_sorted(query, page_rows) if has_next else None
        meta_info = SearchMetaInfo(
            fts_applied=True,
            embedding_applied=False,
            degraded_reason="sort_not_relevance",
        )

    # Step 5 — enrich.
    product_ids = [p.id for p, _b in page_rows]
    wishlisted = await fetch_wishlisted_ids(db, user.id if user else None, product_ids)
    items = [
        to_list_item(p, b, is_wishlisted=p.id in wishlisted) for p, b in page_rows
    ]

    logger.info(
        "search.products.ok q_len=%d sort=%s count=%d total=%d degraded=%s",
        len(query.q),
        query.sort,
        len(items),
        total_candidates,
        meta_info.degraded_reason,
    )
    return ProductsSearchResult(
        items=items,
        total_candidates=total_candidates,
        next_cursor=next_cursor,
        meta_info=meta_info,
    )


async def run_brands_search(
    db: AsyncSession,
    user: User | None,
    q: str,
) -> list[BrandIndexItem]:
    """Brand section — top-5 ILIKE substring match on name / name_ko.
    Description is intentionally excluded: brand matches must come from the
    brand identity, not marketing copy (so `데님` doesn't pull a brand whose
    blurb happens to mention denim).
    """
    needle = q.strip().lower()
    if not needle:
        return []
    pattern = f"%{needle}%"
    prefix_pattern = f"{needle}%"

    # Priority: name prefix > name contains > name_ko contains.
    name_lc = func.lower(Brand.name)
    name_ko_lc = func.lower(func.coalesce(Brand.name_ko, ""))

    score_expr = case(
        (name_lc.like(prefix_pattern), 3),
        (name_lc.like(pattern), 2),
        (name_ko_lc.like(pattern), 1),
        else_=0,
    )
    match_predicate = or_(
        name_lc.like(pattern),
        name_ko_lc.like(pattern),
    )

    recent_cutoff = datetime.now(UTC) - timedelta(days=BRAND_RECENT_CUTOFF_DAYS)
    agg_sq = (
        select(
            Product.brand_id.label("brand_id"),
            func.count(Product.id).filter(Product.is_active).label("product_count"),
            func.count(Product.id)
            .filter(Product.is_active, Product.created >= recent_cutoff)
            .label("new_count"),
        )
        .group_by(Product.brand_id)
        .subquery()
    )

    stmt = (
        select(
            Brand,
            func.coalesce(agg_sq.c.product_count, 0).label("product_count"),
            func.coalesce(agg_sq.c.new_count, 0).label("new_count"),
            score_expr.label("match_score"),
        )
        .outerjoin(agg_sq, agg_sq.c.brand_id == Brand.id)
        .where(Brand.is_active, match_predicate)
        .order_by(score_expr.desc(), Brand.id.desc())
        .limit(BRAND_TOP_K)
    )
    rows = (await db.execute(stmt)).all()
    brand_ids = [r.Brand.id for r in rows]
    followed_ids = await fetch_followed_brand_ids(
        db, user.id if user else None, brand_ids
    )

    return [
        to_brand_index_item(
            r.Brand,
            product_count=int(r.product_count),
            new_count=int(r.new_count),
            is_followed=r.Brand.id in followed_ids,
        )
        for r in rows
    ]


async def get_recent_queries(db: AsyncSession, user_id: UUID) -> list[str]:
    """Recent distinct search queries for the given user (suggestions endpoint)."""
    cutoff = datetime.now(UTC) - timedelta(days=RECENT_SEARCH_WINDOW_DAYS)
    last_used = func.max(SearchEvent.created)
    stmt = (
        select(SearchEvent.query, last_used.label("last_used"))
        .where(SearchEvent.user_id == user_id, SearchEvent.created >= cutoff)
        .group_by(SearchEvent.query)
        .order_by(last_used.desc())
        .limit(RECENT_SEARCH_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    return [r.query for r in rows]


async def log_search_event(
    db: AsyncSession,
    user_id: UUID,
    q: str,
    result_count: int,
) -> None:
    """Fire-and-forget `search_event` insert. Own commit, swallow errors.

    Matches `product.view_event` pattern — must not block the response.
    """
    try:
        await db.execute(
            insert(SearchEvent).values(
                user_id=user_id,
                query=q,
                result_count=result_count,
            )
        )
        await db.commit()
        logger.info(
            "search.event.stored user_id=%s q_len=%d count=%d",
            user_id,
            len(q),
            result_count,
        )
    except Exception as e:  # noqa: BLE001 — swallow all
        logger.warning(
            "search.event.failed user_id=%s q_len=%d err=%s", user_id, len(q), e
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001, S110
            pass


# ---- Internal: relevance branch ----


@dataclass
class _ScoredCandidate:
    product: Product
    brand: Brand
    ts_rank: float
    embedding: list[float] | None
    score: float = 0.0  # filled by rerank step


async def _run_relevance(
    db: AsyncSession,
    query: SearchQuery,
    tsq: Any,
    search_vector: Any,
    effective_price: Any,
    where_clauses: list[Any],
    count_stmt: Any,
) -> tuple[list[_ScoredCandidate], int]:
    """FTS top-K=200 candidates (no cursor filter in SQL) + parallel count."""
    ts_rank = func.ts_rank_cd(search_vector, tsq)
    stmt = (
        select(
            Product,
            Brand,
            ts_rank.label("ts_rank"),
            effective_price.label("effective_price"),
        )
        .join(Brand, Brand.id == Product.brand_id)
        .where(*where_clauses)
        .order_by(ts_rank.desc(), Product.id.desc())
        .limit(FTS_CANDIDATE_LIMIT)
    )
    # Sequential — AsyncSession wraps a single connection; asyncio.gather on two
    # ORM-returning queries can collide via the greenlet shim.
    rows = (await db.execute(stmt)).all()
    total_match = int((await db.execute(count_stmt)).scalar_one() or 0)

    candidates = [
        _ScoredCandidate(
            product=r.Product,
            brand=r.Brand,
            ts_rank=float(r.ts_rank),
            embedding=_coerce_embedding(r.Product.embedding),
        )
        for r in rows
    ]
    return candidates, total_match


async def _run_embedding_fallback(
    db: AsyncSession,
    query: SearchQuery,
    query_emb: list[float],
    effective_price: Any,
    filter_where: list[Any],
) -> list[_ScoredCandidate]:
    """ANN fallback when FTS returned zero candidates.

    Uses pgvector's HNSW cosine_distance (`idx_product_embedding`) with the
    same filters the FTS path would have applied, minus the FTS `@@` match.
    `ts_rank` is zeroed so the combined_score step collapses to embed-only
    (cosine_sim * WEIGHT_EMBED).
    """
    where = [*filter_where, Product.embedding.is_not(None)]
    stmt = (
        select(
            Product,
            Brand,
            effective_price.label("effective_price"),
        )
        .join(Brand, Brand.id == Product.brand_id)
        .where(*where)
        .order_by(Product.embedding.cosine_distance(query_emb))
        .limit(FTS_CANDIDATE_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    # Score each candidate from ANN distance ordering. We synthesise a simple
    # descending score (`1 - rank / N`) so the cursor keyset stays well-ordered
    # even when the reranker is unavailable; the reranker (if it runs) will
    # overwrite these.
    n = len(rows)
    return [
        _ScoredCandidate(
            product=r.Product,
            brand=r.Brand,
            ts_rank=0.0,
            embedding=_coerce_embedding(r.Product.embedding),
            score=1.0 - (i / max(n, 1)),
        )
        for i, r in enumerate(rows)
    ]


async def _apply_fallback_reranker(
    q: str, candidates: list[_ScoredCandidate]
) -> None:
    """Cross-encoder rerank on the top `RERANKER_TOP_N` of the ANN result.

    Overwrites `.score` on the reranked head. The tail (beyond top-N) keeps
    its ANN-derived score, which is always ≤ head scores by construction, so
    the post-sort order is: [reranked head by score DESC] then [ANN tail].

    Fail-open: reranker unavailable → candidates unchanged, ANN order used.
    """
    if not candidates:
        return
    head = candidates[:RERANKER_TOP_N]
    texts = [
        f"{c.product.name} {c.product.description or ''}".strip()[:RERANKER_TEXT_MAX]
        for c in head
    ]
    scores = await reranker_client.rerank(q, texts)
    if scores is None:
        return

    # Rerank scores are 0~1; scale into (1.0, 2.0] so the reranked head strictly
    # outranks the ANN-scored tail (which lives in [0, 1]).
    for c, s in zip(head, scores, strict=True):
        c.score = 1.0 + float(s)


def _rerank_and_slice(
    query: SearchQuery,
    candidates: list[_ScoredCandidate],
    query_emb: list[float] | None,
    *,
    prescored: bool = False,
    target_category: ProductCategory | None = None,
    target_subcategory: str | None = None,
) -> tuple[list[_ScoredCandidate], dict[str, Any] | None, SearchMetaInfo]:
    """Embedding rerank → cursor-filter → slice.

    `query_emb` is the embedding fetched by the caller; `None` means the TEI
    call already failed (fail-open). This function is sync — callers own the
    async embedding fetch so we don't double-call TEI.

    `prescored=True` signals that candidates already carry a meaningful
    `.score` (embedding-only fallback path, optionally reranked). Skip the
    combined_score rerank; jump straight to sort + cursor + slice.
    """
    if not candidates:
        return (
            [],
            None,
            SearchMetaInfo(
                fts_applied=True,
                embedding_applied=False,
                degraded_reason=None,
            ),
        )

    degraded: SearchDegradedReason | None = None
    embedding_applied = False

    if prescored:
        # Fallback path — scores already materialised (ANN distance, optionally
        # overwritten by the reranker head). Just pass through.
        embedding_applied = query_emb is not None
    elif query_emb is None:
        # Fail-open: keep ts_rank order; score = ts_rank (uniform across candidates,
        # but ordering is already by ts_rank DESC so this preserves order).
        degraded = "embedding_unavailable"
        for c in candidates:
            c.score = c.ts_rank
    else:
        embedding_applied = True
        max_ts = max((c.ts_rank for c in candidates), default=0.0)
        q_arr = np.asarray(query_emb, dtype=np.float32)
        q_norm = float(np.linalg.norm(q_arr))
        for c in candidates:
            ts_norm = (c.ts_rank / max_ts) if max_ts > 0 else 0.0
            if c.embedding is not None and q_norm > 0:
                p_arr = np.asarray(c.embedding, dtype=np.float32)
                p_norm = float(np.linalg.norm(p_arr))
                cos_sim = (
                    float(np.dot(q_arr, p_arr) / (q_norm * p_norm))
                    if p_norm > 0
                    else 0.0
                )
            else:
                cos_sim = 0.0
            c.score = WEIGHT_FTS * ts_norm + WEIGHT_EMBED * cos_sim

    # Category / subcategory intent boost — applies on both FTS-path and
    # fallback-path scores. Subcategory match dominates category match.
    if target_subcategory is not None or target_category is not None:
        for c in candidates:
            if target_subcategory is not None and c.product.subcategory == target_subcategory:
                c.score += _SUBCATEGORY_BOOST
            elif target_category is not None and c.product.category == target_category:
                c.score += _CATEGORY_BOOST

    # Sort by (score DESC, id DESC) — matches cursor keyset.
    candidates.sort(key=lambda c: (c.score, str(c.product.id)), reverse=True)

    # Application-layer cursor filter.
    if query.cursor_score is not None and query.cursor_id is not None:
        cursor_score = float(query.cursor_score)
        cursor_id_str = str(query.cursor_id)
        candidates = [
            c
            for c in candidates
            if (c.score, str(c.product.id)) < (cursor_score, cursor_id_str)
        ]

    # Sentinel slice.
    has_next = len(candidates) > query.limit
    page = candidates[: query.limit]

    next_cursor: dict[str, Any] | None = None
    if has_next and page:
        last = page[-1]
        next_cursor = {
            "sort": "relevance",
            "cursor_score": float(last.score),
            "cursor_id": str(last.product.id),
        }

    return (
        page,
        next_cursor,
        SearchMetaInfo(
            fts_applied=True,
            embedding_applied=embedding_applied,
            degraded_reason=degraded,
        ),
    )


# ---- Internal: non-relevance (recent / price_*) branch ----


async def _run_sorted(
    db: AsyncSession,
    query: SearchQuery,
    tsq: Any,
    search_vector: Any,
    effective_price: Any,
    where_clauses: list[Any],
    count_stmt: Any,
) -> tuple[list[tuple[Product, Brand]], int]:
    """Single-SQL page for sort=recent / price_asc / price_desc. Returns
    `limit+1` rows (sentinel) + total match count.
    """
    base = (
        select(Product, Brand)
        .join(Brand, Brand.id == Product.brand_id)
        .where(*where_clauses)
        .limit(query.limit + 1)
    )

    if query.sort == "recent":
        stmt = base.order_by(Product.created.desc(), Product.id.desc())
        if query.cursor_created is not None and query.cursor_id is not None:
            stmt = stmt.where(
                tuple_(Product.created, Product.id)
                < tuple_(literal(query.cursor_created), literal(query.cursor_id))
            )
    elif query.sort == "price_asc":
        stmt = base.order_by(effective_price.asc().nulls_last(), Product.id.desc())
        if query.cursor_price is not None and query.cursor_id is not None:
            stmt = stmt.where(
                or_(
                    effective_price > query.cursor_price,
                    and_(
                        effective_price == query.cursor_price,
                        Product.id < query.cursor_id,
                    ),
                )
            )
    else:  # price_desc
        stmt = base.order_by(effective_price.desc().nulls_last(), Product.id.desc())
        if query.cursor_price is not None and query.cursor_id is not None:
            stmt = stmt.where(
                or_(
                    effective_price < query.cursor_price,
                    and_(
                        effective_price == query.cursor_price,
                        Product.id < query.cursor_id,
                    ),
                )
            )

    rows = (await db.execute(stmt)).all()
    total_match = int((await db.execute(count_stmt)).scalar_one() or 0)
    return [(r.Product, r.Brand) for r in rows], total_match


def _build_next_cursor_sorted(
    query: SearchQuery, page_rows: list[tuple[Product, Brand]]
) -> dict[str, Any] | None:
    if not page_rows:
        return None
    last_product, _ = page_rows[-1]
    if query.sort == "recent":
        return {
            "sort": "recent",
            "cursor_created": last_product.created.isoformat(),
            "cursor_id": str(last_product.id),
        }
    # price_asc | price_desc — effective_price materialized from row.
    eff = (
        last_product.discounted_price
        if last_product.discounted_price is not None
        else last_product.original_price
    )
    return {
        "sort": query.sort,
        "cursor_price": int(eff),
        "cursor_id": str(last_product.id),
    }


# ---- Helpers ----


def _coerce_embedding(value: Any) -> list[float] | None:
    """pgvector returns numpy.ndarray / list / None — normalize to list[float]."""
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        return [float(x) for x in value.tolist()]
    if isinstance(value, list):
        return [float(x) for x in value]
    # Unknown shape — treat as missing (defensive).
    return None


__all__ = [
    "ProductsSearchResult",
    "get_recent_queries",
    "log_search_event",
    "run_brands_search",
    "run_products_search",
]
