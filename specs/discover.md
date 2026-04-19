# Spec: discover

> **spec-handshake 2026-04-19 재확정** — FE↔BE 다회 핸드셰이크 결과 pagination 방식이 opaque cursor → 엔드포인트별 투명 파라미터로 전환, For You는 `user_product_affinity` 테이블 읽기 전용으로 단순화, Popular 필터/Following 빈 브랜드 등 세부 쉐이프가 대폭 변경되었다. 이전 스펙은 전부 무효. 이 문서가 현재 source of truth.

디자인 기준: `design/project/Gyeol Unified.html` — `DiscoverScreen` (L193~), `SavedScreen` (L689~), `ProductDetailScreen` (L1916~).
데이터 참조: `design/project/data.jsx`.
FE shared contract: `docs/plans/discover/api-mapping.md`.

## Summary

Gyeol 디스커버 백엔드 스펙. 1) Discover 4탭(`popular`/`new`/`foryou`/`following`), 2) Product Detail + Related 4개, 3) Saved 화면(wishlist 조회 + 토글) 커버. Popular/New/Detail/Related는 **게스트 OK**, For You/Following/Saved/wishlist mutation은 **`AUTH_REQUIRED`**.

핵심 단순화:
- **For You는 읽기 전용** — `user_product_affinity(user_id, product_id, rank)` 테이블에서 `rank ASC`로 읽어 반환. Seed embedding 계산·HNSW ANN 쿼리는 전부 data pipeline 책임. BE에는 embedding 관련 코드 없음.
- **Popular 정렬은 `product.popularity DESC, id DESC` 키셋** — data pipeline이 `product.popularity` 컬럼을 채운다. 초기엔 0이라 `id DESC`(시간 역순)와 같은 결과.
- **Pagination은 엔드포인트별 투명 파라미터** — opaque cursor(itsdangerous) 전면 폐기. Popular은 `(popularity, id)`, New는 `id`, For You는 `rank`, Wishlist는 `skip/limit`.
- **Popular/New 탭에 필터 없음** — 이번 스콥에서 `category`/`styles`/`fit` 쿼리 전부 제거. 필터 UI는 차기 Filter feature.

Wishlist는 유저당 500개 상한(`WISHLIST_FULL`, 409)에 advisory lock + `ON CONFLICT DO NOTHING`으로 race-safe. View event는 `GET /products/{id}` 내부에서 로그인 시에만 fire-and-forget. 이미지 URL은 `thumbnail_key` / `image_keys[]` / `logo_key`에 `settings.CDN_BASE_URL`을 붙여 응답에 삽입. Envelope은 auth와 동일한 `StandardSuccessResponse`. Pagination cursor는 `meta.next: dict[str, Any] | None` 필드로 반환하며, FE는 이 dict를 그대로 query string에 실어 다음 요청에 붙인다.

## Scope

| # | Method | Path | 기능 |
|---|--------|------|------|
| 1 | `GET` | `/products?tab=popular` | Popular 피드 (keyset: popularity, id) |
| 2 | `GET` | `/products?tab=new` | New 피드 (keyset: id) |
| 3 | `GET` | `/products/foryou` | For You 피드 (auth, affinity 테이블 읽기) |
| 4 | `GET` | `/products/following` | Following 피드 (auth, 페이지네이션 없음) |
| 5 | `GET` | `/products/{product_id}` | 상품 상세 + stats, 로그인 시 view 자동 기록 |
| 6 | `GET` | `/products/{product_id}/related` | 관련 상품 4개 (LIMIT 4 고정) |
| 7 | `GET` | `/wishlist` | Saved 목록 (auth, skip/limit) |
| 8 | `POST` | `/wishlist/{product_id}` | Wishlist 추가 (auth, idempotent) |
| 9 | `DELETE` | `/wishlist/{product_id}` | Wishlist 제거 (auth, idempotent) |

> 파일 내부 설명은 9 섹션이지만 #1/#2는 동일 엔드포인트(`GET /products`)에서 `tab` 파라미터로 분기한다 — 즉 **라우트는 8개**.

---

## 1 & 2. `GET /products`

Popular / New 피드. 단일 라우트에서 `tab`으로 분기. 필터 없음, 정렬·페이지네이션 파라미터만 다르다.

### Auth
선택. 비로그인 OK. 로그인 시 각 아이템에 `is_wishlisted` 채움.

### Request

Query:

| param | 타입 | 필수 | 설명 | 유효 탭 |
|-------|------|------|------|---------|
| `tab` | `Literal["popular", "new"]` | Y | 피드 종류 | 공통 |
| `limit` | `int` (1~40, 기본 20) | N | | 공통 |
| `cursor_popularity` | `float` | N | 직전 응답 `meta.next.cursor_popularity` | `popular`만 |
| `cursor_id` | `UUID` | N | 직전 응답 `meta.next.cursor_id` | 공통 (popular/new 각각 의미 다름) |

- `tab=popular` 첫 페이지 → `cursor_popularity`/`cursor_id` 모두 생략. tie-breaker 목적으로 두 값은 **항상 함께** 보내거나 **함께** 생략.
- `tab=new` 첫 페이지 → `cursor_id` 생략. `cursor_popularity`는 new 탭에 들어오면 422.

```python
# app/schemas/discover.py

class ProductFeedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tab: Literal["popular", "new"]
    limit: int = Field(default=20, ge=1, le=40)
    cursor_popularity: float | None = None
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check_cursor(self) -> "ProductFeedQuery":
        if self.tab == "new" and self.cursor_popularity is not None:
            raise ValueError("cursor_popularity is only valid when tab=popular")
        if self.tab == "popular":
            # popular는 둘 다 있거나 둘 다 없어야 함
            both = (self.cursor_popularity is not None) == (self.cursor_id is not None)
            if not both:
                raise ValueError("cursor_popularity and cursor_id must be paired")
        return self
```

### Response 200 — Popular

```json
{
  "data": {
    "items": [
      {
        "id": "018f...",
        "name": "Volume Wool Trouser",
        "description": "허리선이 높고 밑단으로 갈수록 가볍게 퍼지는 실루엣...",
        "brand": {
          "id": "018f...",
          "slug": "studio-nicholson",
          "name": "STUDIO NICHOLSON",
          "name_ko": null,
          "logo_url": "https://cdn.gyeol.co/brands/018f.../logo.webp"
        },
        "category": "bottom",
        "subcategory": "trouser",
        "sub_category_detail": "trouser-wide",
        "fit": "relaxed",
        "color": "NAVY",
        "pattern": "SOLID",
        "material": ["wool", "cotton"],
        "details": ["high-rise", "pleated"],
        "styles": ["tailored", "minimal"],
        "tags": ["wool", "navy", "high-rise"],
        "thumbnail_url": "https://cdn.gyeol.co/products/018f.../thumb.webp",
        "image_urls": [
          "https://cdn.gyeol.co/products/018f.../image_0.webp",
          "https://cdn.gyeol.co/products/018f.../image_1.webp"
        ],
        "url": "https://musinsa.com/app/goods/...",
        "original_price": 598000,
        "discounted_price": null,
        "discount_rate": 0,
        "is_soldout": false,
        "is_active": true,
        "is_wishlisted": false,
        "created": "2026-04-15T00:00:00Z"
      }
    ]
  },
  "meta": {
    "request_id": "req_...",
    "next": {
      "cursor_popularity": 0.873,
      "cursor_id": "018f-xxxxx"
    }
  }
}
```

### Response 200 — New

```json
{
  "data": { "items": [ /* ProductListItem[] */ ] },
  "meta": {
    "request_id": "req_...",
    "next": { "cursor_id": "018f-xxxxx" }
  }
}
```

더 이상 페이지 없으면 `meta.next: null`.

Popular의 `rank` 필드는 **응답에 없다**. FE가 배열 인덱스 + 1로 `"01", "02"` 표시. (For You의 `rank`와 혼동 금지 — 그건 affinity 테이블 값.)

### Validation Rules

- `tab` 누락 / 지정값 외 → 422 `VALIDATION_ERROR`
- `limit` 범위 밖 → 422
- `tab=new`에 `cursor_popularity` 동반 → 422 (model_validator)
- popular에서 `cursor_popularity`/`cursor_id` 한쪽만 제공 → 422
- `cursor_id` UUID 파싱 실패 → 422

### Data Access

#### Popular (`tab="popular"`)

```sql
SELECT
  p.id, p.name, p.description,
  p.category, p.subcategory, p.sub_category_detail,
  p.fit, p.color, p.pattern, p.material, p.details,
  p.styles, p.tags,
  p.thumbnail_key, p.image_keys, p.url,
  p.original_price, p.discounted_price, p.discount_rate, p.is_soldout,
  p.is_active, p.created, p.popularity,
  b.id AS brand_id, b.slug AS brand_slug, b.name AS brand_name,
  b.name_ko AS brand_name_ko, b.logo_key
FROM product p
JOIN brand b ON b.id = p.brand_id
WHERE p.is_active = true
  AND (
    :cursor_popularity IS NULL
    OR (p.popularity, p.id) < (:cursor_popularity, :cursor_id)
  )
ORDER BY p.popularity DESC, p.id DESC
LIMIT :limit + 1;
```

- `(popularity, id) < (cursor_popularity, cursor_id)` 는 postgres row-constructor 비교 — `popularity = cursor_popularity`일 때 `id`로 tie-break.
- data pipeline이 `popularity`를 채우기 전엔 전부 `0.0` → 자연스럽게 `id DESC`(uuid v7 = 시간 역순)로 정렬.
- `LIMIT :limit + 1`로 다음 cursor 존재 판단. `len(rows) > limit`이면 `rows[-2]` (즉 limit번째 row)의 `popularity, id`를 다음 cursor로 반환하고, 마지막 row는 버리고 `:limit`개만 응답에 담는다.

#### New (`tab="new"`)

```sql
SELECT
  p.id, p.name, p.description,
  p.category, p.subcategory, p.sub_category_detail,
  p.fit, p.color, p.pattern, p.material, p.details,
  p.styles, p.tags,
  p.thumbnail_key, p.image_keys, p.url,
  p.original_price, p.discounted_price, p.discount_rate, p.is_soldout,
  p.is_active, p.created,
  b.id AS brand_id, b.slug AS brand_slug, b.name AS brand_name,
  b.name_ko AS brand_name_ko, b.logo_key
FROM product p
JOIN brand b ON b.id = p.brand_id
WHERE p.is_active = true
  AND (:cursor_id IS NULL OR p.id < :cursor_id)
ORDER BY p.id DESC
LIMIT :limit + 1;
```

- uuid v7 특성상 `id DESC` = `created DESC`. 별도 `created` keyset 불필요.
- 7일 윈도우 제한 **없음** — Discover New 탭은 전부 브라우징. (디자인의 `format-days-ago` 배지는 `item.created`로 FE가 판단.)

#### `is_wishlisted` (로그인 시)

`product.id ANY(:ids)` 로 일괄 조회 → Python set 비교:

```sql
SELECT product_id FROM wishlist
WHERE user_id = :user_id AND product_id = ANY(:product_ids);
```

비로그인은 전부 `false`.

### Indexes 활용

- Popular: `idx_product_popularity (popularity DESC, id DESC) WHERE is_active` — migration 007
- New: `product PK (id)` — 역순 스캔
- `wishlist PK (user_id, product_id)` — `is_wishlisted` 배치 lookup

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | tab/limit/cursor 파라미터 제약 위반 |

### Side Effects
없음.

---

## 3. `GET /products/foryou`

### Auth
필수. 비로그인 → 401 `AUTH_REQUIRED`.

### Request

Query:

| param | 타입 | 필수 | 설명 |
|-------|------|------|------|
| `limit` | `int` (1~40, 기본 20) | N | |
| `cursor_rank` | `int` | N | 직전 응답 `meta.next.cursor_rank` |

첫 페이지 → `cursor_rank` 생략. `cursor_rank=0` 이면 "rank > 0" 부터.

```python
class ForYouQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=40)
    cursor_rank: int | None = Field(default=None, ge=0)
```

### Response 200 — Affinity 있음

```json
{
  "data": {
    "mode": "affinity",
    "items": [
      {
        "id": "018f...",
        "name": "...",
        "description": "...",
        "brand": {
          "id": "...",
          "slug": "...",
          "name": "...",
          "name_ko": null,
          "logo_url": "https://cdn.gyeol.co/brands/.../logo.webp"
        },
        "category": "top",
        "subcategory": "shirt",
        "sub_category_detail": "shirt-oversized",
        "fit": "relaxed",
        "color": "BEIGE",
        "pattern": "SOLID",
        "material": ["cotton"],
        "details": ["drop-shoulder"],
        "styles": ["minimal", "archive"],
        "tags": ["cotton", "beige"],
        "thumbnail_url": "https://cdn.gyeol.co/products/.../thumb.webp",
        "image_urls": ["https://cdn.gyeol.co/products/.../image_0.webp"],
        "url": "https://etcseoul.com/...",
        "original_price": 450000,
        "discounted_price": null,
        "discount_rate": 0,
        "is_soldout": false,
        "is_active": true,
        "is_wishlisted": false,
        "created": "2026-04-10T00:00:00Z",
        "rank": 0
      }
    ]
  },
  "meta": {
    "request_id": "req_...",
    "next": { "cursor_rank": 19 }
  }
}
```

### Response 200 — Fallback (affinity 0건)

유저가 신규이거나 data pipeline이 아직 안 채웠을 때. 내부적으로 Popular 쿼리를 invoke:

```json
{
  "data": {
    "mode": "popular_fallback",
    "items": [ /* Popular 첫 페이지와 동일 shape, 단 rank 필드 없음 */ ]
  },
  "meta": {
    "request_id": "req_...",
    "next": {
      "cursor_popularity": 0.873,
      "cursor_id": "018f-xxxxx"
    }
  }
}
```

Fallback에서는 `mode="popular_fallback"`으로 FE가 빈-상태 배너를 띄운다 ("둘러보면 채워집니다"). Pagination은 Popular 규칙(`cursor_popularity` + `cursor_id`) 그대로. FE는 `mode`가 `popular_fallback`인 동안은 `cursor_popularity`/`cursor_id`를, `affinity`인 동안은 `cursor_rank`를 다음 요청에 보낸다 — 두 mode 간 페이지 사이 전환은 **없다** (첫 페이지 결정 후 유지).

### Validation

- `limit` 범위 밖 → 422
- `cursor_rank < 0` → 422

### Data Access

#### 1. Affinity 조회

```sql
SELECT
  p.id, p.name, p.description,
  p.category, p.subcategory, p.sub_category_detail,
  p.fit, p.color, p.pattern, p.material, p.details,
  p.styles, p.tags,
  p.thumbnail_key, p.image_keys, p.url,
  p.original_price, p.discounted_price, p.discount_rate, p.is_soldout,
  p.is_active, p.created,
  b.id AS brand_id, b.slug AS brand_slug, b.name AS brand_name,
  b.name_ko AS brand_name_ko, b.logo_key,
  upa.rank
FROM user_product_affinity upa
JOIN product p ON p.id = upa.product_id AND p.is_active = true
JOIN brand b ON b.id = p.brand_id
WHERE upa.user_id = :user_id
  AND (:cursor_rank IS NULL OR upa.rank > :cursor_rank)
ORDER BY upa.rank ASC
LIMIT :limit + 1;
```

- `idx_user_product_affinity_order (user_id, rank)` 완전 활용.
- JOIN에서 product가 deactivate되면 자연스럽게 제외 — 응답 아이템 수가 `:limit`보다 적게 나올 수 있음 (사이즈 공백 허용). staleness 대응은 data pipeline 책임.
- 다음 cursor = `rows[limit-1].rank` (즉 마지막으로 반환하는 row의 rank). `len(rows) > limit`이 아니면 `meta.next: null`.

#### 2. Fallback 판정

**첫 페이지**(`cursor_rank IS NULL`)에서만 fallback 판정. 쿼리 결과 0건이면 내부적으로 Popular service 호출. `cursor_rank`가 있는 후속 페이지에서 0건이 나오면 "affinity 끝"이므로 `items: []` + `next: null`로 반환 (fallback 발동 안 함).

#### 3. `is_wishlisted`

Popular과 동일하게 배치 lookup.

### Indexes

- `idx_user_product_affinity_order` — migration 007
- `wishlist PK` — `is_wishlisted`

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | |
| `ACCOUNT_DEACTIVATED` | 403 | |
| `VALIDATION_ERROR` | 422 | |

### Side Effects
없음.

### Notes

- BE는 seed vector 계산·HNSW `<=>` 쿼리를 **전혀 수행하지 않는다**. 모든 추천 정렬은 `user_product_affinity.rank`가 이미 결정해둔다.
- affinity 테이블 staleness 체크(last rebuild 시간 비교, 만료시 popular fallback 전환 등)도 BE 범위 밖. data pipeline이 주기적으로 rebuild.

---

## 4. `GET /products/following`

### Auth
필수.

### Request

Query: 없음.

### Response 200

```json
{
  "data": {
    "total_new": 14,
    "brands": [
      {
        "brand": {
          "id": "018f...",
          "slug": "studio-nicholson",
          "name": "STUDIO NICHOLSON",
          "name_ko": null,
          "logo_url": "https://cdn.gyeol.co/brands/018f.../logo.webp",
          "product_added": "2026-04-18T10:00:00Z"
        },
        "new_count": 3,
        "items": [ /* ProductListItem[], 최대 6개 */ ]
      }
    ]
  },
  "meta": { "request_id": "req_..." }
}
```

- **Pagination 없음** — 한 번에 모든 팔로우 브랜드(신상 있는 것만)를 반환. `meta.next` 필드 없음.
- 팔로우했지만 7일 내 신상 **없는** 브랜드는 `brands[]`에 **포함하지 않는다** — 이전 스펙 대비 변경.
- `total_new` = 현재 응답의 모든 `new_count` 합.
- `window_days` 고정 7, `per_brand` 고정 6 (디자인 기준). 쿼리 파라미터로 노출하지 않음.
- `brand.product_added` = 해당 브랜드의 가장 최신 product.created (섹션 헤더의 "N days ago"용).

### Validation
없음 (파라미터 자체가 없음).

### Data Access

#### 1. 팔로우 브랜드 중 7일 내 신상 있는 것만 + 브랜드별 집계

```sql
SELECT
  b.id AS brand_id, b.slug AS brand_slug, b.name AS brand_name,
  b.name_ko AS brand_name_ko, b.logo_key,
  n.new_count, n.product_added
FROM brand_follow bf
JOIN brand b ON b.id = bf.brand_id AND b.is_active = true
JOIN LATERAL (
  SELECT count(*) AS new_count, max(p.created) AS product_added
  FROM product p
  WHERE p.brand_id = b.id
    AND p.is_active = true
    AND p.created >= now() - interval '7 days'
) n ON n.new_count > 0
WHERE bf.user_id = :user_id
ORDER BY n.product_added DESC, b.id DESC;
```

`JOIN LATERAL ... ON n.new_count > 0` 로 7일 내 신상 없는 브랜드 자연 제외.

#### 2. 브랜드별 신상 6개 pluck (단일 쿼리)

```sql
SELECT * FROM (
  SELECT
    p.id, p.name, p.description,
    p.category, p.subcategory, p.sub_category_detail,
    p.fit, p.color, p.pattern, p.material, p.details,
    p.styles, p.tags,
    p.thumbnail_key, p.image_keys, p.url,
    p.original_price, p.discounted_price, p.discount_rate, p.is_soldout,
    p.is_active, p.created, p.brand_id,
    row_number() OVER (
      PARTITION BY p.brand_id
      ORDER BY p.id DESC
    ) AS rn
  FROM product p
  WHERE p.brand_id = ANY(:brand_ids)
    AND p.is_active = true
    AND p.created >= now() - interval '7 days'
) x
WHERE rn <= 6;
```

Python에서 `brand_id`로 group. 각 item의 `brand` 필드는 1번 쿼리에서 얻은 `BrandSummary`로 동일하게 채운다 (추가 JOIN 불필요).

#### 3. `is_wishlisted` 배치 lookup.

### Indexes

- `brand_follow PK (user_id, brand_id)` — 1번 쿼리 prefix
- `idx_product_brand WHERE is_active` — 2번 브랜드별 필터
- `(brand_id, created DESC)` 인덱스는 없음 — 초기 볼륨에선 brand당 수십~수백 row 이내, in-memory sort 수용 가능. 수만 row 넘어갈 때 재검토.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | |
| `ACCOUNT_DEACTIVATED` | 403 | |

팔로우 0개 또는 7일 내 신상 0개 → `brands: []`, `total_new: 0`, 200 OK.

### Side Effects
없음.

---

## 5. `GET /products/{product_id}`

### Auth
선택. 게스트 OK. 로그인 시 `product_event` view INSERT 자동(fire-and-forget).

### Request

Path: `product_id: UUID`.

### Response 200

```json
{
  "data": {
    "id": "018f...",
    "name": "Volume Wool Trouser",
    "description": "허리선이 높고...",
    "brand": {
      "id": "018f...",
      "slug": "studio-nicholson",
      "name": "STUDIO NICHOLSON",
      "name_ko": null,
      "logo_url": "https://cdn.gyeol.co/brands/018f.../logo.webp",
      "description": "...",
      "website_url": "https://studionicholson.com",
      "is_followed": false
    },
    "category": "bottom",
    "subcategory": "trouser",
    "sub_category_detail": "trouser-wide",
    "fit": "relaxed",
    "color": "NAVY",
    "pattern": "SOLID",
    "material": ["wool", "cotton"],
    "details": ["high-rise", "pleated"],
    "styles": ["tailored", "minimal"],
    "tags": ["wool", "navy", "high-rise"],
    "thumbnail_url": "https://cdn.gyeol.co/products/018f.../thumb.webp",
    "image_urls": [
      "https://cdn.gyeol.co/products/018f.../image_0.webp",
      "https://cdn.gyeol.co/products/018f.../image_1.webp"
    ],
    "url": "https://musinsa.com/app/goods/...",
    "original_price": 598000,
    "discounted_price": null,
    "discount_rate": 0,
    "is_soldout": false,
    "is_active": true,
    "is_wishlisted": false,
    "created": "2026-04-15T00:00:00Z",
    "stats": {
      "view_count_7d": 4280,
      "wishlist_count": 312
    }
  },
  "meta": { "request_id": "req_..." }
}
```

- `brand` = `BrandFull` (Summary + `description`/`website_url`/`is_followed`).
- `brand.is_followed` — 게스트 `false` 고정.
- `stats` = nested object.

### Data Access

#### 1. 상품 + 브랜드

```sql
SELECT
  p.id, p.name, p.description,
  p.category, p.subcategory, p.sub_category_detail,
  p.fit, p.color, p.pattern, p.material, p.details,
  p.styles, p.tags,
  p.thumbnail_key, p.image_keys,
  p.url, p.original_price, p.discounted_price, p.discount_rate,
  p.is_soldout, p.is_active, p.created, p.brand_id,
  b.slug AS brand_slug, b.name AS brand_name, b.name_ko,
  b.description AS brand_description,
  b.logo_key, b.website_url
FROM product p
JOIN brand b ON b.id = p.brand_id
WHERE p.id = :product_id
  AND p.is_active = true;
```

Row 없음 → `PRODUCT_NOT_FOUND` (404). `is_active=false`도 동일 404 (FE 노출 없음).

#### 2. 로그인 시 추가 조회 (병렬)

```sql
-- is_wishlisted
SELECT EXISTS(
  SELECT 1 FROM wishlist
  WHERE user_id = :user_id AND product_id = :product_id
);

-- is_followed
SELECT EXISTS(
  SELECT 1 FROM brand_follow
  WHERE user_id = :user_id AND brand_id = :brand_id
);
```

#### 3. Stats (병렬)

```sql
-- view_count_7d
SELECT count(*) FROM product_event
WHERE product_id = :product_id AND type = 'view'
  AND created >= now() - interval '7 days';

-- wishlist_count
SELECT count(*) FROM wishlist WHERE product_id = :product_id;
```

`asyncio.gather`로 2, 3번 쿼리 병렬. wishlist_count는 `idx_wishlist_product` 활용. view_count는 TimescaleDB chunk scan.

#### 4. View event 기록 (로그인 시만, fire-and-forget)

```sql
INSERT INTO product_event (user_id, type, product_id, properties)
VALUES (:user_id, 'view', :product_id, '{"source": "detail"}'::jsonb);
```

- 응답 커밋 이후 별도 작업으로 수행. 실패 시 `logger.warning` 후 swallow. `product_event.user_id NOT NULL` 제약상 게스트는 INSERT 자체 스킵.
- Dedup(60초 내 동일 user × product skip)은 이번 스펙 out-of-scope.

### Validation

- `product_id` UUID 파싱 실패 → 422 `VALIDATION_ERROR`.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `PRODUCT_NOT_FOUND` | 404 | 상품 없음 또는 `is_active=false` |
| `VALIDATION_ERROR` | 422 | UUID 아님 |

### Side Effects

- 로그인 시만 `product_event` 1 row INSERT (view). 실패 무시.

---

## 6. `GET /products/{product_id}/related`

### Auth
선택.

### Request

Path: `product_id: UUID`.
Query: 없음. `limit`은 **서버 고정 4**.

### Response 200

```json
{
  "data": {
    "items": [ /* ProductListItem[] × 4, is_wishlisted 포함 */ ]
  },
  "meta": { "request_id": "req_..." }
}
```

Pagination 없음. `meta.next` 필드 없음.

### Data Access

#### 1. Seed product 조회

```sql
SELECT brand_id, embedding
FROM product
WHERE id = :product_id AND is_active = true;
```

없음 → `PRODUCT_NOT_FOUND`.

#### 2. Primary (embedding 있음)

```sql
SELECT
  p.id, p.name, p.description,
  p.category, p.subcategory, p.sub_category_detail,
  p.fit, p.color, p.pattern, p.material, p.details,
  p.styles, p.tags,
  p.thumbnail_key, p.image_keys, p.url,
  p.original_price, p.discounted_price, p.discount_rate, p.is_soldout,
  p.is_active, p.created,
  b.id AS brand_id, b.slug AS brand_slug, b.name AS brand_name,
  b.name_ko AS brand_name_ko, b.logo_key,
  (p.brand_id = :seed_brand_id) AS same_brand
FROM product p
JOIN brand b ON b.id = p.brand_id
WHERE p.id <> :product_id
  AND p.is_active = true
  AND p.embedding IS NOT NULL
ORDER BY
  (p.brand_id = :seed_brand_id) DESC,
  p.embedding <=> :seed_embedding ASC,
  p.id DESC
LIMIT 4;
```

- 단일 쿼리, LIMIT 4 고정. HNSW index + boolean sort 조합이라 index 직접 활용은 제한적이지만 4개 뽑는 작업이므로 full scan이어도 부담 미미.
- 최적화(두 쿼리 병합, 같은 브랜드 + 다른 브랜드 분리)는 이번 스펙 out-of-scope.

#### 3. Degraded (seed `embedding IS NULL`)

```sql
SELECT
  p.id, /* ... 공통 컬럼 ... */,
  (p.brand_id = :seed_brand_id) AS same_brand
FROM product p
JOIN brand b ON b.id = p.brand_id
WHERE p.id <> :product_id AND p.is_active = true
ORDER BY
  (p.brand_id = :seed_brand_id) DESC,
  p.id DESC
LIMIT 4;
```

같은 브랜드 우선, 이후 최신순. embedding 없이도 화면을 비우지 않도록 최소 fallback.

#### 4. `is_wishlisted` 배치 lookup (로그인 시).

### Indexes

- `idx_product_embedding` (HNSW, cosine) — migration 001
- `idx_product_brand WHERE is_active` — 필터

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `PRODUCT_NOT_FOUND` | 404 | seed 없음 / 비활성 |
| `VALIDATION_ERROR` | 422 | UUID 형식 |

### Side Effects
없음.

---

## 7. `GET /wishlist`

### Auth
필수.

### Request

Query:

| param | 타입 | 필수 | 설명 |
|-------|------|------|------|
| `category` | `ProductCategory \| None` | N | Saved 카테고리 필터 |
| `skip` | `int` (0~, 기본 0) | N | offset |
| `limit` | `int` (1~100, 기본 100) | N | 500 cap이라 기본 크게 |

```python
class WishlistListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ProductCategory | None = None
    skip: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=100, ge=1, le=100)
```

### Response 200

```json
{
  "data": {
    "count_total": 27,
    "count_by_category": {
      "outer": 4,
      "top": 10,
      "bottom": 8,
      "acc": 2,
      "shoes": 3
    },
    "items": [
      {
        "id": "018f...",
        "name": "...",
        "brand": { "id": "...", "slug": "...", "name": "...", "name_ko": null, "logo_url": "..." },
        "category": "top",
        "thumbnail_url": "...",
        "image_urls": ["..."],
        "url": "...",
        "original_price": 450000,
        "discounted_price": null,
        "discount_rate": 0,
        "is_soldout": false,
        "is_active": true,
        "is_wishlisted": true,
        "created": "2026-03-30T00:00:00Z",
        "saved_at": "2026-04-10T12:00:00Z",
        "styles": ["minimal"],
        "tags": ["cotton"],
        "description": "...",
        "subcategory": "tee",
        "sub_category_detail": "tee-boxy",
        "fit": "relaxed",
        "color": "BEIGE",
        "pattern": "SOLID",
        "material": ["cotton"],
        "details": ["boxy-fit"]
      }
    ]
  },
  "meta": {
    "request_id": "req_...",
    "next": { "skip": 100 }
  }
}
```

- `count_total` = 유저의 전체 wishlist 개수 (category 필터 무관). 500 cap 게이지용.
- `count_by_category` = 카테고리별 분포 (filter 상관없이 전체). 없는 카테고리는 0으로 채워 5개 key 전부 포함.
- `items`: `saved_at DESC, product_id DESC` 정렬.
- `is_wishlisted` 항상 `true`.

더 이상 페이지 없으면 `meta.next: null`.

### Validation

- `skip < 0` / `limit` 범위 밖 → 422
- `category` enum 외 → 422

### Data Access

#### 1. 페이지 + saved_at

```sql
SELECT
  p.id, p.name, p.description,
  p.category, p.subcategory, p.sub_category_detail,
  p.fit, p.color, p.pattern, p.material, p.details,
  p.styles, p.tags,
  p.thumbnail_key, p.image_keys, p.url,
  p.original_price, p.discounted_price, p.discount_rate, p.is_soldout,
  p.is_active, p.created,
  b.id AS brand_id, b.slug AS brand_slug, b.name AS brand_name,
  b.name_ko AS brand_name_ko, b.logo_key,
  w.created AS saved_at
FROM wishlist w
JOIN product p ON p.id = w.product_id AND p.is_active = true
JOIN brand b ON b.id = p.brand_id
WHERE w.user_id = :user_id
  AND (:category IS NULL OR p.category = :category)
ORDER BY w.created DESC, w.product_id DESC
OFFSET :skip
LIMIT :limit + 1;
```

- 다음 `skip` = `현재 skip + limit`. `len(rows) > limit`이면 `meta.next: {"skip": current_skip + limit}`, 아니면 `null`.
- 500 cap이라 offset 페이지네이션 부담 없음 (offset 깊이가 최대 500).

#### 2. Count totals (병렬)

```sql
-- count_total
SELECT count(*) FROM wishlist WHERE user_id = :user_id;

-- count_by_category (active product만 카운트)
SELECT p.category, count(*)
FROM wishlist w
JOIN product p ON p.id = w.product_id AND p.is_active = true
WHERE w.user_id = :user_id
GROUP BY p.category;
```

Python에서 빈 카테고리를 0으로 채움.

### Indexes

- `wishlist PK (user_id, product_id)` — user 필터 prefix
- `idx_wishlist_product` — 역방향 (`count_by_category`용)

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | |
| `ACCOUNT_DEACTIVATED` | 403 | |
| `VALIDATION_ERROR` | 422 | |

### Side Effects
없음.

---

## 8. `POST /wishlist/{product_id}`

### Auth
필수.

### Request

Path: `product_id: UUID`. Body 없음.

### Response 200

```json
{
  "data": {
    "product_id": "018f...",
    "is_wishlisted": true,
    "wishlist_count": 42
  },
  "meta": { "request_id": "req_..." }
}
```

- 중복 호출 시 200 no-op — 기존 row 유지, `wishlist_count` 변화 없음.
- 500 cap 도달 상태에서 신규 추가 요청 → 409 `WISHLIST_FULL`. 이미 있는 상품을 다시 add 호출하는 경우는 cap 무관하게 200 (상태 변화 없음).

### Data Access

단일 트랜잭션, advisory lock:

```sql
BEGIN;

-- 유저별 advisory lock
SELECT pg_advisory_xact_lock(hashtext('wishlist:' || :user_id::text));

-- 1. product 존재 + active
SELECT 1 FROM product WHERE id = :product_id AND is_active = true;
-- 없으면 ROLLBACK + PRODUCT_NOT_FOUND

-- 2. 이미 있는지
SELECT 1 FROM wishlist
WHERE user_id = :user_id AND product_id = :product_id;
-- 있으면 count만 조회하고 idempotent 리턴 (event 기록 안 함)

-- 3. cap 검사
SELECT count(*) FROM wishlist WHERE user_id = :user_id;
-- >= 500 → ROLLBACK + WISHLIST_FULL (detail_payload: {"cap": 500, "current": 500})

-- 4. INSERT wishlist
INSERT INTO wishlist (user_id, product_id) VALUES (:user_id, :product_id);

-- 5. dual-write product_event
INSERT INTO product_event (user_id, type, product_id, properties)
VALUES (:user_id, 'wishlist_add', :product_id, '{}'::jsonb);

-- 6. 최종 count
SELECT count(*) FROM wishlist WHERE user_id = :user_id;

COMMIT;
```

Advisory lock 해제는 트랜잭션 커밋 시 자동. 다른 유저엔 영향 없음.

### Validation

- `product_id` UUID 위반 → 422

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | |
| `ACCOUNT_DEACTIVATED` | 403 | |
| `PRODUCT_NOT_FOUND` | 404 | 상품 없음 또는 비활성 |
| `WISHLIST_FULL` | 409 | 500 cap 도달 + 신규 추가 시도. `detail_payload: {"cap": 500, "current": 500}` |
| `VALIDATION_ERROR` | 422 | UUID 위반 |

### Side Effects

- 신규 INSERT 시: `wishlist` 1 row + `product_event(type='wishlist_add')` 1 row.
- 중복 호출 시: no DB write.

---

## 9. `DELETE /wishlist/{product_id}`

### Auth
필수.

### Request

Path: `product_id: UUID`. Body 없음.

### Response 200

```json
{
  "data": {
    "product_id": "018f...",
    "is_wishlisted": false,
    "wishlist_count": 41
  },
  "meta": { "request_id": "req_..." }
}
```

POST와 완전 동일 shape (`WishlistToggleResponse`). `is_wishlisted`만 반대.

### Data Access

```sql
BEGIN;

DELETE FROM wishlist
WHERE user_id = :user_id AND product_id = :product_id
RETURNING product_id;

-- RETURNING 결과 있음 → 실제 삭제 발생 → event 기록
INSERT INTO product_event (user_id, type, product_id, properties)
VALUES (:user_id, 'wishlist_remove', :product_id, '{}'::jsonb);
-- (RETURNING 비었으면 skip)

SELECT count(*) FROM wishlist WHERE user_id = :user_id;

COMMIT;
```

Advisory lock **불필요** — DELETE는 단일 row 연산, cap 검사 없음.
`product.is_active=false`여도 wishlist에서는 삭제 허용 (cleanup).

### Validation

- `product_id` UUID 위반 → 422

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | |
| `ACCOUNT_DEACTIVATED` | 403 | |
| `VALIDATION_ERROR` | 422 | UUID 위반 |

**`PRODUCT_NOT_FOUND` 던지지 않음** — DELETE는 없는 것에 대해서도 성공 (idempotent).

### Side Effects

- 실제 삭제 시: `wishlist` 1 row DELETE + `product_event(type='wishlist_remove')` 1 row.
- 없던 상태에서 DELETE 시: no DB write.

---

## Shared Concerns

### Envelope + `meta.next` 규약

모든 응답은 `StandardSuccessResponse[T]` 재사용. 기존 `StandardResponseMeta`에 pagination용 `next` 필드 한 개 추가:

```python
# app/core/envelope.py (수정 필요)

class StandardResponseMeta(BaseModel):
    request_id: str
    error: ErrorMeta | None = None
    next: dict[str, Any] | None = None  # pagination — FE는 이 dict를 query string으로 붙여 다음 요청
```

- `next` 키는 pagination이 있는 엔드포인트에서만 값이 들어간다. Pagination 없는 엔드포인트(`/products/following`, `/products/{id}`, `/products/{id}/related`)는 응답 직렬화 시 `next=None` → Pydantic `exclude_none` 미적용 상태라면 `"next": null`로 나간다 (FE는 `null` 체크).
- 더 이상 페이지 없으면 `next: null`.
- 타입별:
  - Popular: `{"cursor_popularity": float, "cursor_id": "uuid-string"}`
  - New: `{"cursor_id": "uuid-string"}`
  - For You (affinity): `{"cursor_rank": int}`
  - For You (popular_fallback): `{"cursor_popularity": float, "cursor_id": "uuid-string"}`
  - Wishlist: `{"skip": int}`

FE는 `next` dict를 `URLSearchParams` 로 전개해서 다음 요청에 그대로 붙이면 된다 — cursor 해석/서명 검증 없음.

### Pydantic DTOs

```python
# app/schemas/discover.py

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProductCategory = Literal["outer", "top", "bottom", "acc", "shoes"]


class BrandSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    slug: str
    name: str
    name_ko: str | None = None
    logo_url: str | None = None  # absolute CDN URL


class BrandFull(BrandSummary):
    description: str | None = None
    website_url: str | None = None
    is_followed: bool = False
    # Following 섹션 전용 — Detail에선 None
    product_added: datetime | None = None


class ProductListItem(BaseModel):
    """Popular/New/ForYou/Following/Related/Saved 공통 rich 리스트 아이템."""
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    name: str
    description: str | None = None
    brand: BrandSummary
    category: ProductCategory
    subcategory: str | None = None
    sub_category_detail: str | None = None
    fit: str | None = None
    color: str | None = None
    pattern: str | None = None
    material: list[str]
    details: list[str]
    styles: list[str]
    tags: list[str]
    thumbnail_url: str
    image_urls: list[str]
    url: str
    original_price: int
    discounted_price: int | None = None
    discount_rate: float
    is_soldout: bool
    is_active: bool
    is_wishlisted: bool = False
    created: datetime
    # 엔드포인트별 옵셔널
    rank: int | None = None          # For You affinity 에서만 노출
    saved_at: datetime | None = None # Wishlist 전용


class ProductStats(BaseModel):
    view_count_7d: int
    wishlist_count: int


class ProductDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    name: str
    description: str | None = None
    brand: BrandFull
    category: ProductCategory
    subcategory: str | None = None
    sub_category_detail: str | None = None
    fit: str | None = None
    color: str | None = None
    pattern: str | None = None
    material: list[str]
    details: list[str]
    styles: list[str]
    tags: list[str]
    thumbnail_url: str
    image_urls: list[str]
    url: str
    original_price: int
    discounted_price: int | None = None
    discount_rate: float
    is_soldout: bool
    is_active: bool
    is_wishlisted: bool = False
    created: datetime
    stats: ProductStats


class ProductListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ProductListItem]


class ForYouResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["affinity", "popular_fallback"]
    items: list[ProductListItem]


class FollowingBrandBlock(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    brand: BrandFull
    new_count: int
    items: list[ProductListItem]


class FollowingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_new: int
    brands: list[FollowingBrandBlock]


class RelatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ProductListItem]


class WishlistListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count_total: int
    count_by_category: dict[ProductCategory, int]
    items: list[ProductListItem]


class WishlistToggleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    product_id: UUID
    is_wishlisted: bool
    wishlist_count: int
```

`discount_rate` — DB는 `numeric GENERATED`. ORM에서 `Decimal` → `float` 변환 시 소수 4자리 반올림 권장.

### Image URL 합성

DB 저장: `thumbnail_key = "products/{product_id}/thumb.webp"`, `image_keys = ["products/{id}/image_0.webp", ...]`, `logo_key = "brands/{brand_id}/logo.webp"`.

API 응답:
```python
thumbnail_url = f"{settings.CDN_BASE_URL}/{thumbnail_key}"
image_urls = [f"{settings.CDN_BASE_URL}/{k}" for k in image_keys]
logo_url = f"{settings.CDN_BASE_URL}/{logo_key}" if logo_key else None
```

환경변수 `CDN_BASE_URL` 이 `app/core/config.py`에 필요 (trailing slash 없이, 예: `https://cdn.gyeol.co`).

### 인증 deps

- `OptionalUser = Annotated[User | None, Depends(get_optional_user)]` — Popular/New/Detail/Related
- `CurrentUser = Annotated[User, Depends(require_auth)]` — For You, Following, Wishlist 전체

auth 스펙과 동일 인터페이스 재사용.

### View event 정책

| 엔드포인트 | 이벤트 | 조건 |
|-----------|--------|------|
| `GET /products/{id}` | `view` | 로그인 시만. fire-and-forget. |
| `POST /wishlist/{id}` | `wishlist_add` | dual-write (신규 INSERT 시만) |
| `DELETE /wishlist/{id}` | `wishlist_remove` | dual-write (실제 DELETE 시만) |

`product_event.user_id NOT NULL` 제약상 게스트 이벤트는 INSERT 자체 스킵. Dedup (60초 내 동일 user × product view skip)은 이번 스펙 범위 외 — 추후 `POST /events` batched feature 또는 ETL.

### Error Enum (AppErrorCodeEnum)

본 스펙에서 사용하는 코드 — 전부 **이미 `app/core/errors.py`에 정의되어 있음** (auth 스펙에서 보강됨):

| Code | HTTP |
|------|------|
| `VALIDATION_ERROR` | 422 |
| `AUTH_REQUIRED` | 401 |
| `ACCOUNT_DEACTIVATED` | 403 |
| `PRODUCT_NOT_FOUND` | 404 |
| `WISHLIST_FULL` | 409 |

신규 enum 추가 **불필요**.

### Route 구조

```
app/api/routes/
├── auth.py       (기존)
├── product.py    (신규) — #1~#6 모두. GET /products, /products/foryou, /products/following, /products/{id}, /products/{id}/related
└── wishlist.py   (신규) — #7~#9. GET/POST/DELETE /wishlist/{...}
```

product 도메인 라우트를 파일 하나로 통합 — list·detail·feed가 전부 product 중심이라 파일 분리 이득이 크지 않다. wishlist는 state mutation이라 별도 파일.

### Service 구조

```
app/services/
├── auth.py           (기존)
├── product.py        (신규) — get_by_id, get_related, get_popular, get_new, get_following
├── foryou.py         (신규) — get(user_id, cursor_rank, limit). affinity 쿼리 + popular fallback 호출.
└── wishlist.py       (신규) — add, remove, list. advisory lock·cap·dual-write.
```

Route는 입력 파싱 + service 호출 + envelope wrap만. SQL은 service 안.

### Logging

- `logging.getLogger("gyeol.product")` / `"gyeol.foryou"` / `"gyeol.wishlist"`.
- 이벤트: `product.detail.ok`, `foryou.affinity.hit`, `foryou.fallback.popular`, `wishlist.add.ok|duplicate|full`, `wishlist.remove.ok|noop`.
- 에러는 `logger.exception` (stack trace 보존).

---

## Schema Alignment

`data/migrations/001_init.up.sql` + `007_discover_support.up.sql` 기준.

| 스펙 사용 | 스키마 | 비고 |
|-----------|--------|------|
| `product.{id, brand_id, name, description, thumbnail_key, image_keys, category, subcategory, sub_category_detail, fit, color, pattern, material, details, styles, tags, original_price, discounted_price, discount_rate, embedding, is_active, is_soldout, url, created, popularity}` | ✓ | `popularity` = migration 007 |
| `brand.{id, slug, name, name_ko, description, logo_key, website_url, is_active}` | ✓ | |
| `wishlist.{user_id, product_id, created}` PK | ✓ | 앱단 500 cap |
| `brand_follow.{user_id, brand_id, created}` PK | ✓ | |
| `product_event.{id, user_id, type, product_id, properties, created}` | ✓ | TimescaleDB hypertable |
| `user_product_affinity.{user_id, product_id, rank, created}` PK `(user_id, product_id)` | ✓ | migration 007 — BE 읽기 전용 |
| `idx_product_popularity (popularity DESC, id DESC) WHERE is_active` | ✓ | migration 007 |
| `idx_user_product_affinity_order (user_id, rank)` | ✓ | migration 007 |
| `idx_product_embedding` (HNSW, cosine) | ✓ | Related에서 사용 |

**Schema gaps: 0건.** 신규 마이그레이션 불필요.

---

## Out of Scope

1. `POST /events` batched 이벤트 수신 — 별도 feature
2. `/brands/{slug}` / `/brands/{slug}/products` / brand follow mutation — brand feature
3. `/search` 검색 — search feature
4. Popular/New 탭 필터 (`category`/`styles`/`fit`) — 추후 Filter feature
5. Related LIMIT 변경 / 다양성 부스트 — 이번 스펙은 고정 4개
6. View event dedup (60s 내 동일 user × product skip)
7. For You affinity staleness 판정 / 주기 rebuild — data pipeline 책임
8. For You seed embedding 계산 / HNSW ANN — data pipeline 책임
9. Recommendation `reason` 생성
10. Wishlist 폴더 / 태그 분류
11. Rate limiting / ETag / 304
12. Sliding session renewal
13. Admin product CRUD (ETL 파이프라인이 채움)
14. Popular/Following infinite scroll UX — FE가 "Load more" 버튼으로만 진행

---

## Decision Log

> 이전 스펙의 결정을 아래로 축약·갱신. 이번 핸드셰이크에서 **변경된** 항목은 맨 뒤에 명시.

| # | Decision | Why |
|---|----------|-----|
| 1 | Rich list item (ProductListItem) 공통 | FE가 detail round-trip 안 하고 리스트 카드 즉시 렌더 |
| 2 | Wishlist toggle 응답 대칭 `{product_id, is_wishlisted, wishlist_count}` | 토글 UX와 1:1 매핑, 비대칭 `saved_at`/`removed` 제거 |
| 3 | Wishlist add idempotent 200 (중복 시 no-op) | 네트워크 재시도 친화 |
| 4 | Wishlist cap 500 — advisory lock 기반 race-safe | 솔로 DB 환경에서 lock 부담 없음 |
| 5 | View event 자동 (GET /products/{id} 내부) | FE 별도 호출 제거, 게스트는 스킵 |
| 6 | Image URL은 BE가 CDN absolute 완성형 반환 | FE 합성 없음, 단일 출처 |
| 7 | Following 섹션은 brand-grouped 구조 유지 | 디자인이 명시적으로 brand 블록 |
| 8 | Error 코드 `PRODUCT_NOT_FOUND`(404), `WISHLIST_FULL`(409), inactive도 404 통일 | 구체·일관 |
| 9 | Detail `stats`는 nested object | `stats.view_count_7d`, `stats.wishlist_count` |
| 10 | **Pagination은 엔드포인트별 투명 파라미터** (opaque cursor 폐기) | FE 디버깅/구현 단순, 재서명/서명키 회전 부담 제거 |
| 11 | **`meta.next: dict[str, Any] \| None`** — FE는 dict를 query string으로 전개 | 엔드포인트별 키 다름(popularity/id/rank/skip) 자연 수용 |
| 12 | **For You는 `user_product_affinity` 읽기 전용** — seed embedding 계산·ANN 쿼리 BE에서 제거 | 책임 분리, data pipeline이 rebuild 주기 관리 |
| 13 | **Popular/New 탭에 `category`/`styles`/`fit` 필터 없음** | 이번 스콥에선 필터 UI 미구현, 차기 Filter feature |
| 14 | **Following은 paginate 안 함 + 빈 브랜드 생략** | 팔로우 브랜드 수가 적음, 7일 신상 없는 브랜드 섹션 없애서 화면 깔끔 |
| 15 | **Related는 단일 쿼리 + LIMIT 4 고정** | 최적화(두 쿼리 분리)는 스케일 발생 시 재검토 |

---

## Checklist

- [x] 8 routes (9 논리적 엔드포인트 — #1/#2 단일 라우트 `tab` 분기)
- [x] Auth/request/response/validation/data-access/indexes/error/side-effects 각 섹션 기재
- [x] `meta.next` dict 규약 정의 + 엔드포인트별 키 표기
- [x] Pydantic DTO 전체 (BrandSummary/BrandFull/ProductListItem/ProductStats/ProductDetail/FollowingBrandBlock + 응답 wrapper)
- [x] Image URL 합성 규칙
- [x] Error enum: 신규 추가 0건 (기존 enum 재사용)
- [x] Schema alignment: migration 007 반영, 신규 테이블/인덱스 0건
- [x] Route/service 구조
- [x] Out of Scope 14건
- [x] Decision Log 15건 (변경 항목 #10~#15 하이라이트)
