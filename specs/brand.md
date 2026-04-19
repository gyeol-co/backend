# Spec: brand

> **spec-handshake 2026-04-19** — Brand Index (정렬 3종 + 스타일 필터), Brand Detail (커버·메타·관련 브랜드 3개 embed), Brand Products 그리드(카테고리 필터 + keyset), Follow mutation(idempotent POST/DELETE)까지 총 5개 엔드포인트. 디자인 기준 `design/project/brand-views.jsx`(`GyeolBrandIndex` L43, `GyeolBrandDetail` L277). Pagination 규약은 discover 스펙(§Shared Concerns §meta.next) 그대로 계승 — 엔드포인트별 투명 cursor, `meta.next: dict[str, Any] | None`.

디자인 기준: `design/project/brand-views.jsx`.
DB 현황: migration 001/009 — `brand` + `platform` 포함. `brand_follow` M:N. `brand_event` hypertable + `brand_event_type` enum(`view/click/follow/unfollow`).

## Summary

Gyeol 브랜드 기능 백엔드 스펙. 1) Brand Index(20개 그리드 + Featured 섹션 + sort·style), 2) Brand Detail(프로필 + 메타 + 관련 브랜드 3개), 3) Brand Products(해당 브랜드 상품 그리드, 카테고리 필터), 4) Brand Follow / Unfollow(idempotent). Index / Detail / Products는 **게스트 OK**(로그인 시 `is_followed`·`is_wishlisted` 채움). Follow mutation은 **로그인 필수**(`AUTH_REQUIRED`).

핵심 단순화:

- **Brand DTO 재사용** — `BrandSummary`/`BrandFull`(이미 `app/schemas/discover.py`에 존재)을 그대로 계승. Brand feature는 aggregate(`product_count`, `new_count`, `last_update`)와 `cover_url` 2개만 추가 필드로 얹은 파생 DTO를 쓴다.
- **Follow cap 없음** — Wishlist 500 cap과 달리 Follow는 무제한. advisory lock 불필요(멱등 INSERT · DELETE만).
- **Pagination은 엔드포인트별 투명 파라미터** — discover 스펙과 동일 규약. sort별로 keyset 키가 다르다(alpha: `(name, id)`, featured: 2-phase 후 내부 `(is_featured_int, product_added, id)`, recent: `(product_added, id)`). `meta.next`는 `dict[str, Any] | None`, FE는 그대로 query string 전개.
- **Related brands**는 Detail 응답에 3개 embed. 페이지네이션 없음. 같은 `styles` 태그 최소 1개 공유 + `product_added DESC NULLS LAST, id DESC` 상위 3개.
- **Follow event dual-write** — `brand_follow` row + `brand_event(type='follow'|'unfollow')` 1건 같은 트랜잭션에서 커밋. Wishlist dual-write와 동일 패턴. advisory lock 없음(`ON CONFLICT DO NOTHING` + `RETURNING`으로 no-op 판정).

## Scope

| # | Method | Path | 기능 |
|---|--------|------|------|
| 1 | `GET` | `/brands` | Index — sort(alpha/featured/recent) + style 필터, cursor pagination |
| 2 | `GET` | `/brands/{slug}` | Detail — 프로필 + 집계 + related 3개 embed |
| 3 | `GET` | `/brands/{slug}/products` | 브랜드 상품 그리드 — category 필터, keyset `(popularity, id)` |
| 4 | `POST` | `/brands/{slug}/follow` | Follow 추가 (auth, idempotent) |
| 5 | `DELETE` | `/brands/{slug}/follow` | Follow 제거 (auth, idempotent) |

---

## 1. `GET /brands`

Brand Index — 20개 내외 브랜드 그리드. sort 3종 + style 필터 조합. 디자인 L43~L204.

### Auth
선택. 비로그인 OK. 로그인 시 각 아이템에 `is_followed` 채움.

### Request

Query:

| param | 타입 | 필수 | 설명 | 유효 sort |
|-------|------|------|------|-----------|
| `sort` | `Literal["alpha","featured","recent"]` | N (기본 `alpha`) | 정렬 종류 | 공통 |
| `style` | `list[str]` (repeat) | N | 스타일 태그 필터. 값 0개=전체. `brand.styles && :styles` (겹치면 매치) | 공통 |
| `limit` | `int` (1~40, 기본 24) | N | | 공통 |
| `cursor_name` | `str` | N | 직전 응답 `meta.next.cursor_name`. `sort=alpha`에서만 유효 | `alpha`만 |
| `cursor_product_added` | `datetime` | N | 직전 `meta.next.cursor_product_added`. `sort=recent`/`featured`에서만 유효. 생략 가능. `product_added IS NULL` phase는 `cursor_product_added` 키 없이 `cursor_id` 만으로 표현(명시적 null 값 전송 금지). | `recent`, `featured` |
| `cursor_is_featured` | `bool` | N | `sort=featured`에서만 유효. 2-phase: `true` phase 소진 후 `false` phase로 넘어갈 때 `false` 로 cursor 전환 | `featured`만 |
| `cursor_id` | `UUID` | N | 모든 sort의 tie-breaker | 공통 |

- `sort=alpha` 첫 페이지 → `cursor_name`/`cursor_id` 생략. keyset: `(name ASC, id ASC)` — 둘 다 있거나 둘 다 없어야 함(422).
- `sort=recent` 첫 페이지 → `cursor_product_added`/`cursor_id` 생략. keyset: `(product_added DESC NULLS LAST, id DESC)`. `product_added IS NULL` 뒤꼬리는 `cursor_product_added` 키 생략 + `cursor_id` 로 진입 — FE는 dict 그대로 전달하고 BE는 `cursor_product_added` 필드의 존재 여부로 phase 판정(아래 Pydantic model_validator 참조).
- `sort=featured` 첫 페이지 → 세 cursor 전부 생략. 내부 keyset: `(is_featured DESC, product_added DESC NULLS LAST, id DESC)`. 응답 `meta.next`는 현재 phase의 `cursor_is_featured`와 `cursor_id`를 내려 주고, 해당 row의 `product_added`가 non-null일 때만 `cursor_product_added` 키를 포함한다(null phase에서는 키 생략).

```python
# app/schemas/brand.py

class BrandIndexQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sort: Literal["alpha", "featured", "recent"] = "alpha"
    style: list[str] = Field(default_factory=list)
    limit: int = Field(default=24, ge=1, le=40)

    # alpha
    cursor_name: str | None = None
    # recent / featured
    cursor_product_added: datetime | None = None
    # featured 전용
    cursor_is_featured: bool | None = None
    # 공통 tie-breaker
    cursor_id: UUID | None = None

    # Wire format은 "cursor_product_added 없음" vs "있고 값" 2 가지뿐.
    # FE/BE 공통 규약 — tail phase(NULLS LAST, product_added IS NULL) 진입은
    # cursor_product_added 키를 생략하고 cursor_id 만 전달하는 요청으로 표현한다.
    # Pydantic 필드는 default=None 이므로 "키 생략" == "None" 으로 자연 매핑된다.

    @model_validator(mode="after")
    def _check(self) -> "BrandIndexQuery":
        if self.sort == "alpha":
            if (self.cursor_name is None) != (self.cursor_id is None):
                raise ValueError("cursor_name and cursor_id must both be present or absent")
            if self.cursor_product_added is not None or self.cursor_is_featured is not None:
                raise ValueError("cursor_product_added / cursor_is_featured not valid for sort=alpha")
        elif self.sort == "recent":
            if self.cursor_is_featured is not None:
                raise ValueError("cursor_is_featured not valid for sort=recent")
            # (cursor_product_added, cursor_id) — 둘 다 없거나, cursor_id 만 있거나(tail),
            # 둘 다 있거나 셋 중 하나. cursor_product_added만 있고 cursor_id 없는 건 invalid.
            if self.cursor_product_added is not None and self.cursor_id is None:
                raise ValueError("cursor_product_added requires cursor_id")
            if self.cursor_name is not None:
                raise ValueError("cursor_name not valid for sort=recent")
        else:  # featured
            if self.cursor_name is not None:
                raise ValueError("cursor_name not valid for sort=featured")
            # featured cursor는 "phase 진입 이후"에만 존재. 즉 cursor_is_featured 가
            # 있으면 cursor_id 도 반드시 있어야 함.
            if self.cursor_is_featured is not None and self.cursor_id is None:
                raise ValueError("cursor_is_featured requires cursor_id")
            if self.cursor_product_added is not None and self.cursor_is_featured is None:
                raise ValueError("cursor_product_added requires cursor_is_featured for sort=featured")
        return self
```

### Response 200

```json
{
  "data": {
    "items": [
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
    ]
  },
  "meta": {
    "request_id": "req_...",
    "next": {
      "cursor_name": "STUDIO NICHOLSON",
      "cursor_id": "018f-xxxx"
    }
  }
}
```

- `product_count` = 해당 브랜드의 `is_active=true` 상품 수
- `new_count` = 최근 7일 내(`now() - interval '7 days'`) `is_active=true` 상품 수
- `product_added` = 브랜드 row의 `brand.product_added`(크롤러가 채움). `null` 가능.
- `cover_url` = `cover_key`가 있으면 `get_s3().public_url(cover_key)`, 없으면 `null`. 디자인(`BrandCover` L8)은 현재 플레이스홀더지만 실제 데이터 유입 시 API 응답으로 그대로 사용.

`meta.next` 형태 — sort별 (meta.next는 cursor 키만 포함. `sort`/`style`은 FE URL 소스로 판정):

- `sort=alpha`: `{"cursor_name": str, "cursor_id": uuid}`
- `sort=recent`: `{"cursor_product_added": iso-str, "cursor_id": uuid}` — `product_added IS NULL`에 진입한 이후의 다음 페이지는 `{"cursor_id": uuid}` (`cursor_product_added` 키 자체 생략)
- `sort=featured`: `{"cursor_is_featured": bool, "cursor_product_added": iso-str, "cursor_id": uuid}` — 해당 row의 `product_added`가 null이면 `cursor_product_added` 키 자체 생략. featured=true phase 소진하고 첫 false phase 요청 만들 때는 `cursor_is_featured: false` + 마지막 row의 `product_added`(있으면 키 포함, 없으면 생략) + `cursor_id: <last>`.
- `sort`/`style`은 응답 dict에 포함하지 않는다. FE가 현재 URL 쿼리 스트링의 sort/style 파라미터를 그대로 유지한 채 cursor 키만 merge 하여 다음 요청을 구성한다.

더 이상 페이지 없으면 `meta.next: null`.

### Validation
- `sort` enum 외 → 422
- `limit` 범위 밖 → 422
- sort-별 cursor 제약 위반 → 422 (위 `_check` 검증)
- `cursor_id` UUID 파싱 실패 → 422

### Data Access

#### Base query (sort 무관)

```sql
SELECT
  b.id, b.slug, b.name, b.name_ko, b.logo_key, b.cover_key, b.platform,
  b.styles, b.is_featured, b.is_active, b.product_added,
  cnt.product_count, cnt.new_count
FROM brand b
LEFT JOIN LATERAL (
  SELECT
    count(*) FILTER (WHERE p.is_active)                                 AS product_count,
    count(*) FILTER (WHERE p.is_active AND p.created >= :recent_cutoff) AS new_count
  FROM product p
  WHERE p.brand_id = b.id
) cnt ON true
WHERE b.is_active = true
  AND (:styles_empty OR b.styles && :styles_arr)
  /* ... sort-별 cursor 조건 ... */
ORDER BY /* sort-별 */
LIMIT :limit + 1;
```

- `LATERAL`은 브랜드 수가 100개 미만 수준이면 부담 없음. 스케일 커지면 `product` 쪽에 `brand_id`별 summary 캐시 테이블로 교체.
- `:styles_empty` = Python에서 `style` 쿼리가 비었는지 bool. `b.styles && :styles_arr` 는 `idx_brand_styles` (gin) 활용.

#### Sort-별 ORDER BY / cursor

**alpha** — `(name ASC, id ASC)`:

```sql
-- 첫 페이지
ORDER BY b.name ASC, b.id ASC

-- 후속 페이지
AND (b.name, b.id) > (:cursor_name, :cursor_id)
ORDER BY b.name ASC, b.id ASC
```

**recent** — `(product_added DESC NULLS LAST, id DESC)`:

Postgres의 `NULLS LAST` 는 row-constructor 비교로 깔끔하게 안 되므로 2-phase로 쪼개서 Python에서 판단:

```sql
-- Phase A: product_added IS NOT NULL
WHERE ... AND b.product_added IS NOT NULL
  AND (:cursor_product_added IS NULL
       OR (b.product_added, b.id) < (:cursor_product_added, :cursor_id))
ORDER BY b.product_added DESC, b.id DESC
LIMIT :limit + 1;

-- Phase B: product_added IS NULL (tail — cursor_product_added 가 요청에서 없고 cursor_id 만 있을 때)
WHERE ... AND b.product_added IS NULL
  AND (:cursor_id IS NULL OR b.id < :cursor_id)
ORDER BY b.id DESC
LIMIT :limit + 1;
```

라우트 로직(의사 코드):

```
# Pydantic 필드 기준: cursor_product_added=None 은 "키 생략" 과 동치.
# 첫 페이지(cursor 둘 다 None) → phase A. cursor_id 만 있고 cursor_product_added is None → phase B.
# cursor_product_added 가 있으면 phase A 계속.
if cursor_product_added is not None:
    phase = "A"  # phase A 후속
elif cursor_id is not None:
    phase = "B"  # phase B 진입 (null tail)
else:
    phase = "A"  # 첫 페이지
rows = run_phase_A()
if len(rows) < limit:
    # phase A 소진 → phase B 로 보충. phase A 의 나머지(없음)와 phase B 를 합쳐 limit+1 만큼.
    extra = run_phase_B(limit - len(rows) + 1)
    combined = rows + extra
    has_next = len(combined) > limit
    page = combined[:limit]
    if has_next:
        last = page[-1]
        next = {"cursor_id": str(last.id)}
        if last.product_added is not None:
            next["cursor_product_added"] = last.product_added.isoformat()
        # sort/style 은 meta.next 에 포함하지 않는다 — FE 가 현재 URL 에서 유지.
```

Phase B 단독 페이지(요청에 cursor_product_added 없고 cursor_id 만 있음) → Phase B 만 돌리고 끝.

**featured** — `(is_featured DESC, product_added DESC NULLS LAST, id DESC)`:

2-phase 접근으로 featured=true 먼저, 소진되면 featured=false. 각 phase 내부는 위 recent 와 동일(NULLS LAST 처리 같은 2단 쪼개기).

```sql
-- Phase: is_featured = :phase_flag
WHERE ... AND b.is_featured = :phase_flag
  AND b.product_added IS NOT NULL
  AND (:cursor_product_added IS NULL
       OR (b.product_added, b.id) < (:cursor_product_added, :cursor_id))
ORDER BY b.product_added DESC, b.id DESC
LIMIT :limit + 1;
```

- 첫 페이지 진입: `cursor_is_featured`/`cursor_product_added`/`cursor_id` 전부 생략 — `phase_flag=true` 로 시작, `true` phase 의 non-null product_added 부터.
- 해당 phase 의 non-null 소진되면 같은 phase_flag 로 null tail(위 Phase B 유형) 진행.
- true phase 전체(non-null + null) 소진되면 `phase_flag=false` 로 전환. `meta.next` 에 `cursor_is_featured=false` 와 `cursor_id: <last row>` 만 내려주고 `cursor_product_added` 키는 생략한다(첫 false phase 진입 시그널 = non-null product_added 부터 시작).
- true 소진 판정: 현재 phase_flag=true 에서 `limit+1` 보다 적게 나오고 해당 phase 의 null tail 도 소진했으면 다음 cursor는 false phase.

초기엔 is_featured=true 브랜드가 소수(~10개 이내)라서 2-phase 전환은 대부분 첫/둘째 페이지에서 일어난다.

**featured 4개 short-list** (디자인 L168 "Featured this week" 섹션)은 별도 엔드포인트 없이 `sort=featured&limit=4` 첫 페이지로 FE가 커버(또는 클라이언트가 sort=alpha 결과에서 `is_featured` 플래그로 필터). BE는 `is_featured` 플래그만 정확히 반환하면 된다.

#### `is_followed` 일괄 조회 (로그인 시)

```sql
SELECT brand_id FROM brand_follow
WHERE user_id = :user_id AND brand_id = ANY(:brand_ids);
```

결과 Python set 과 각 item 매치. 비로그인은 전부 `false`.

### Indexes 활용

- `idx_brand_styles` (gin) — `styles &&` 필터
- `idx_brand_featured (is_featured) WHERE is_active` — featured phase 분리
- `idx_brand_product_added (product_added DESC NULLS LAST) WHERE is_active` — recent/featured sort
- `brand PK (id)` — alpha/featured tie-break
- `idx_product_brand (brand_id) WHERE is_active` — LATERAL aggregate
- `brand_follow PK (user_id, brand_id)` — `is_followed` batch

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | sort/cursor/limit/style UUID 위반 |

### Side Effects
없음.

---

## 2. `GET /brands/{slug}`

Brand Detail — 프로필 + 집계 + related 3개 embed. 디자인 L277~L529.

### Auth
선택. 게스트 OK. 로그인 시 `brand.is_followed` 채움.

### Request

Path: `slug: str`(unique, url-safe).
Query: 없음.

### Response 200

```json
{
  "data": {
    "id": "018f...",
    "slug": "studio-nicholson",
    "name": "STUDIO NICHOLSON",
    "name_ko": null,
    "logo_url": "https://cdn.gyeol.co/brands/018f.../logo.webp",
    "cover_url": "https://cdn.gyeol.co/brands/018f.../cover.webp",
    "platform": "musinsa",
    "description": "런던 기반 테일러드 무드의 브랜드...",
    "website_url": "https://studionicholson.com",
    "styles": ["minimal", "tailored"],
    "is_featured": true,
    "is_followed": false,
    "product_count": 42,
    "new_count": 3,
    "product_added": "2026-04-16T10:00:00Z",
    "related_brands": [
      {
        "id": "018f...",
        "slug": "lemaire",
        "name": "LEMAIRE",
        "name_ko": null,
        "logo_url": "...",
        "cover_url": null,
        "platform": "etcseoul",
        "styles": ["minimal", "workwear"],
        "is_featured": true,
        "is_followed": true,
        "product_count": 38,
        "new_count": 0,
        "product_added": "2026-04-10T00:00:00Z"
      }
    ]
  },
  "meta": { "request_id": "req_..." }
}
```

- `related_brands` = 같은 `styles` 태그 최소 1개 공유하는 active 브랜드 중 `product_added DESC NULLS LAST, id DESC` 상위 3개. 결과가 0~2개인 경우 그만큼만 반환(빈 배열 허용). 페이지네이션 없음.
- 각 related item도 Index item과 동일 shape(`BrandIndexItem`) — `product_count`/`new_count`/`is_followed` 포함.

### Validation
- `slug`는 경로 파라미터(str 기본 검증).

### Data Access

#### 1. 대상 브랜드 + aggregate

```sql
SELECT
  b.id, b.slug, b.name, b.name_ko, b.logo_key, b.cover_key, b.platform,
  b.description, b.website_url, b.styles, b.is_featured, b.is_active,
  b.product_added,
  cnt.product_count, cnt.new_count
FROM brand b
LEFT JOIN LATERAL (
  SELECT
    count(*) FILTER (WHERE p.is_active)                                 AS product_count,
    count(*) FILTER (WHERE p.is_active AND p.created >= :recent_cutoff) AS new_count
  FROM product p
  WHERE p.brand_id = b.id
) cnt ON true
WHERE b.slug = :slug
  AND b.is_active = true;
```

row 없음 → `BRAND_NOT_FOUND` (404, detail_payload `{"slug": ":slug"}`). `is_active=false`도 동일 404.

#### 2. Related brands (같은 styles, 상위 3개)

```sql
SELECT
  b.id, b.slug, b.name, b.name_ko, b.logo_key, b.cover_key, b.platform,
  b.styles, b.is_featured, b.product_added,
  cnt.product_count, cnt.new_count
FROM brand b
LEFT JOIN LATERAL (
  SELECT
    count(*) FILTER (WHERE p.is_active)                                 AS product_count,
    count(*) FILTER (WHERE p.is_active AND p.created >= :recent_cutoff) AS new_count
  FROM product p
  WHERE p.brand_id = b.id
) cnt ON true
WHERE b.id <> :seed_brand_id
  AND b.is_active = true
  AND b.styles && :seed_styles
ORDER BY b.product_added DESC NULLS LAST, b.id DESC
LIMIT 3;
```

seed 브랜드의 `styles`가 비었으면(`[]`) `&&`가 항상 false가 되어 0개 반환 — 정상 동작. 디자인 동기("비슷한 결의 브랜드", L496)에 맞는 자연 degrade.

#### 3. `is_followed` (로그인 시)

```sql
SELECT brand_id FROM brand_follow
WHERE user_id = :user_id
  AND brand_id = ANY(:all_brand_ids);
```

`all_brand_ids` = seed + related(최대 4개). Python set matching.

#### 4. `recent_cutoff`

```python
recent_cutoff = datetime.now(UTC) - timedelta(days=7)
```

1, 2번 쿼리 `asyncio.gather`로 병렬. 3번은 결과 받은 뒤 brand_id 모아서 한 번에 실행.

### Indexes 활용

- `brand.slug UNIQUE` — lookup
- `idx_brand_styles (gin)` — related `&&`
- `idx_brand_product_added` — related ORDER BY
- `idx_product_brand WHERE is_active` — LATERAL aggregate
- `brand_follow PK (user_id, brand_id)` — follow batch

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `BRAND_NOT_FOUND` | 404 | slug 없음 또는 `is_active=false` |

### Side Effects
없음. (Follow event만 POST/DELETE에서. Brand view event는 out-of-scope.)

---

## 3. `GET /brands/{slug}/products`

브랜드 단위 상품 그리드. Popular 탭과 같은 keyset(`popularity DESC, id DESC`). 디자인 L434~L481.

### Auth
선택. 게스트 OK. 로그인 시 `is_wishlisted` 채움.

### Request

Path: `slug: str`.
Query:

| param | 타입 | 필수 | 설명 |
|-------|------|------|------|
| `category` | `ProductCategory \| None` | N | `outer/top/bottom/acc/shoes` |
| `limit` | `int` (1~40, 기본 24) | N | |
| `cursor_popularity` | `float` | N | 직전 `meta.next.cursor_popularity` |
| `cursor_id` | `UUID` | N | 직전 `meta.next.cursor_id` |

- 첫 페이지 → cursor 둘 다 생략. 둘 다 있거나 둘 다 없어야 함(422).

```python
class BrandProductsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ProductCategory | None = None
    limit: int = Field(default=24, ge=1, le=40)
    cursor_popularity: float | None = None
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> "BrandProductsQuery":
        both_present = self.cursor_popularity is not None and self.cursor_id is not None
        both_absent  = self.cursor_popularity is None and self.cursor_id is None
        if not (both_present or both_absent):
            raise ValueError("cursor_popularity and cursor_id must both be present or absent")
        return self
```

### Response 200

```json
{
  "data": {
    "items": [ /* ProductListItem[] (schemas/discover.py 재사용) */ ],
    "count_total": 42,
    "count_by_category": { "outer": 4, "top": 10, "bottom": 8, "acc": 2, "shoes": 3 }
  },
  "meta": {
    "request_id": "req_...",
    "next": {
      "cursor_popularity": 0.873,
      "cursor_id": "018f-xxxx"
    }
  }
}
```

- `ProductListItem`은 `app/schemas/discover.py`에 이미 정의된 타입 그대로 재사용(rank/saved_at 필드는 항상 `null`).
- `count_total`/`count_by_category`는 filter 무관하게 **해당 브랜드 전체**. 디자인 L450 `{filteredProducts.length} of {products.length}` 표현 재현용.
- `next`는 cursor 키(`cursor_popularity` + `cursor_id`) 만. `category` 필터는 FE URL 소스에서 판정 (§C-1 handshake 결정).

### Validation
- `slug` 미존재 → 404 `BRAND_NOT_FOUND`
- `category` enum 외 → 422
- cursor 쌍 위반 → 422

### Data Access

#### 0. Brand 존재 확인 — Detail과 공유 혹은 단독 slug → id

```sql
SELECT b.id FROM brand b
WHERE b.slug = :slug AND b.is_active = true;
-- 없으면 BRAND_NOT_FOUND
```

#### 1. Page items

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
  b.name_ko AS brand_name_ko, b.logo_key, b.platform
FROM product p
JOIN brand b ON b.id = p.brand_id
WHERE p.brand_id = :brand_id
  AND p.is_active = true
  AND (:category IS NULL OR p.category = :category)
  AND (
    :cursor_popularity IS NULL
    OR (p.popularity, p.id) < (:cursor_popularity, :cursor_id)
  )
ORDER BY p.popularity DESC, p.id DESC
LIMIT :limit + 1;
```

#### 2. Counts (병렬)

```sql
-- count_total
SELECT count(*) FROM product
WHERE brand_id = :brand_id AND is_active = true;

-- count_by_category
SELECT p.category, count(*)
FROM product p
WHERE p.brand_id = :brand_id AND p.is_active = true
GROUP BY p.category;
```

0번 + 1번 + 2번 3개 다 `asyncio.gather`. Python에서 빈 카테고리 0으로 채워 5개 key 항상 포함.

#### 3. `is_wishlisted` — discover 스펙과 동일 배치

```sql
SELECT product_id FROM wishlist
WHERE user_id = :user_id AND product_id = ANY(:ids);
```

### Indexes 활용

- `idx_product_brand (brand_id) WHERE is_active` — brand 필터
- `idx_product_popularity (popularity DESC, id DESC) WHERE is_active` — keyset (brand_id 필터와 조합 시 bitmap and). 브랜드당 상품이 수백 수준이면 단순 seq + sort도 부담 없음.
- `idx_product_category WHERE is_active` — category 필터
- `wishlist PK (user_id, product_id)` — batch

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `BRAND_NOT_FOUND` | 404 | slug 없음/비활성 |
| `VALIDATION_ERROR` | 422 | category/cursor/limit |

### Side Effects
없음.

---

## 4. `POST /brands/{slug}/follow`

### Auth
필수. 비로그인 → 401 `AUTH_REQUIRED`.

### Request

Path: `slug: str`. Body 없음.

### Response 200

```json
{
  "data": {
    "brand_id": "018f...",
    "slug": "studio-nicholson",
    "is_followed": true,
    "follower_count": 1284
  },
  "meta": { "request_id": "req_..." }
}
```

- 중복 호출 시 200 no-op — 기존 row 유지, `follower_count` 변화 없음, event 기록 안 함.
- Follow cap **없음**.

### Data Access

단일 트랜잭션, `ON CONFLICT DO NOTHING` + `RETURNING` 으로 no-op 판정. advisory lock **불필요** — cap 검사가 없으므로 동시성 race 없음.

```sql
BEGIN;

-- 1. brand 확인
SELECT id FROM brand WHERE slug = :slug AND is_active = true;
-- 없으면 ROLLBACK + BRAND_NOT_FOUND

-- 2. INSERT (idempotent)
INSERT INTO brand_follow (user_id, brand_id)
VALUES (:user_id, :brand_id)
ON CONFLICT (user_id, brand_id) DO NOTHING
RETURNING brand_id;

-- 3. 실제 INSERT 된 경우에만 event dual-write
-- (RETURNING 결과 있음 → 신규 follow. 비었음 → 이미 팔로우 중)
INSERT INTO brand_event (user_id, type, brand_id, properties)
VALUES (:user_id, 'follow', :brand_id, '{}'::jsonb);
-- (no-op 케이스에서는 skip)

-- 4. 최종 follower_count
SELECT count(*) FROM brand_follow WHERE brand_id = :brand_id;

COMMIT;
```

### Validation
- `slug` 경로 파라미터 정상만.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | 세션 쿠키 없음/만료 |
| `ACCOUNT_DEACTIVATED` | 403 | is_active=false |
| `BRAND_NOT_FOUND` | 404 | slug 없음/비활성 |

### Side Effects

- 신규 INSERT 시: `brand_follow` 1 row + `brand_event(type='follow')` 1 row.
- 중복 호출 시: no DB write.

---

## 5. `DELETE /brands/{slug}/follow`

### Auth
필수.

### Request
Path: `slug: str`. Body 없음.

### Response 200

POST와 동일 shape (`BrandFollowResponse`), `is_followed: false`.

```json
{
  "data": {
    "brand_id": "018f...",
    "slug": "studio-nicholson",
    "is_followed": false,
    "follower_count": 1283
  },
  "meta": { "request_id": "req_..." }
}
```

### Data Access

```sql
BEGIN;

-- 1. brand lookup — 없던 팔로우를 DELETE 시도할 수도 있으니 BRAND_NOT_FOUND 는 던지지 않는다.
--    slug 로 brand_id 를 찾을 수 없으면 404 (wishlist DELETE와 다른 취급 — wishlist 는 product 존재 검증 안 하지만,
--    여긴 slug → brand_id 매핑이 반드시 필요).
SELECT id FROM brand WHERE slug = :slug AND is_active = true;
-- 없으면 ROLLBACK + BRAND_NOT_FOUND

-- 2. DELETE (idempotent)
DELETE FROM brand_follow
WHERE user_id = :user_id AND brand_id = :brand_id
RETURNING brand_id;

-- 3. RETURNING 결과 있음 → 실제 DELETE → event 기록
INSERT INTO brand_event (user_id, type, brand_id, properties)
VALUES (:user_id, 'unfollow', :brand_id, '{}'::jsonb);
-- (RETURNING 비었으면 skip)

-- 4. 최종 follower_count
SELECT count(*) FROM brand_follow WHERE brand_id = :brand_id;

COMMIT;
```

Wishlist DELETE와의 차이 — slug → brand_id 매핑 자체가 성립해야 하므로 `BRAND_NOT_FOUND` 404는 던진다(wishlist는 product 검증 생략). 브랜드가 active여도 사용자가 팔로우 안 한 상태에서 DELETE → 200 no-op.

### Validation
- `slug` 정상.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | |
| `ACCOUNT_DEACTIVATED` | 403 | |
| `BRAND_NOT_FOUND` | 404 | slug 없음/비활성 |

### Side Effects

- 실제 삭제 시: `brand_follow` 1 row DELETE + `brand_event(type='unfollow')` 1 row.
- 없던 상태 DELETE 시: no DB write.

---

## Shared Concerns

### Envelope + `meta.next`

discover 스펙(§Shared Concerns) 그대로. 브랜드 feature 엔드포인트별 next 키 (meta.next는 cursor 키만 — `sort`/`style`은 FE URL 쿼리 스트링 소스로 판정하여 merge):

- `/brands` alpha: `{"cursor_name": str, "cursor_id": uuid-str}`
- `/brands` recent: `{"cursor_product_added": iso-str, "cursor_id": uuid-str}` — `cursor_product_added` 키는 마지막 row의 `product_added`가 non-null일 때만 포함. null이면 키 자체 생략.
- `/brands` featured: `{"cursor_is_featured": bool, "cursor_product_added": iso-str, "cursor_id": uuid-str}` — `cursor_product_added` 키는 non-null일 때만 포함.
- `/brands/{slug}/products`: `{"cursor_popularity": float, "cursor_id": uuid-str, "category": enum-value | omitted}`

FE는 dict 그대로 `URLSearchParams` 전개 후 현재 URL의 `sort`/`style` 파라미터와 merge. Pagination 없는 #2/#4/#5는 `next: null`.

### Pydantic DTOs

```python
# app/schemas/brand.py

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.product import ProductCategory
from app.schemas.discover import ProductListItem  # products 엔드포인트에서 재사용


# ---- Request queries ----

class BrandIndexQuery(BaseModel):
    """GET /brands — sort/style/cursor 조합 전체."""

    model_config = ConfigDict(extra="forbid")

    sort: Literal["alpha", "featured", "recent"] = "alpha"
    style: list[str] = Field(default_factory=list)
    limit: int = Field(default=24, ge=1, le=40)
    cursor_name: str | None = None
    cursor_product_added: datetime | None = None
    cursor_is_featured: bool | None = None
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> "BrandIndexQuery":
        # (본문 §1 Request 참조 — 여기 동일 로직)
        ...


class BrandProductsQuery(BaseModel):
    """GET /brands/{slug}/products."""

    model_config = ConfigDict(extra="forbid")

    category: ProductCategory | None = None
    limit: int = Field(default=24, ge=1, le=40)
    cursor_popularity: float | None = None
    cursor_id: UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> "BrandProductsQuery":
        both_present = self.cursor_popularity is not None and self.cursor_id is not None
        both_absent  = self.cursor_popularity is None and self.cursor_id is None
        if not (both_present or both_absent):
            raise ValueError("cursor_popularity and cursor_id must both be present or absent")
        return self


# ---- Response DTOs ----

class BrandIndexItem(BaseModel):
    """Index / Detail / Related 공통 rich 브랜드 카드.

    `discover.BrandSummary` 와 구분되는 점:
    - cover_url 추가
    - is_featured 추가
    - styles (array) 추가
    - product_count / new_count 집계 추가
    - product_added (브랜드 row) 그대로 노출
    - is_followed — 로그인 시 채움
    """
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    name_ko: str | None = None
    logo_url: str | None = None
    cover_url: str | None = None
    platform: str | None = None
    styles: list[str]
    is_featured: bool
    is_followed: bool = False
    product_count: int
    new_count: int
    product_added: datetime | None = None


class BrandIndexResponse(BaseModel):
    items: list[BrandIndexItem]


class BrandDetailResponse(BrandIndexItem):
    """Detail은 Index item + description/website_url + related embed."""

    description: str | None = None
    website_url: str | None = None
    related_brands: list[BrandIndexItem]


class BrandProductsResponse(BaseModel):
    items: list[ProductListItem]
    count_total: int
    count_by_category: dict[str, int]


class BrandFollowResponse(BaseModel):
    """POST/DELETE 공용. is_followed 만 반대."""
    model_config = ConfigDict(from_attributes=True)

    brand_id: UUID
    slug: str
    is_followed: bool
    follower_count: int
```

### Image URL 합성

`cover_url` 추가 외에는 discover 스펙 동일. `brand.logo_key` / `brand.cover_key` 에 `get_s3().public_url(key)` 적용. 두 컬럼 모두 nullable 이므로 `None` 가드 후 `None` 반환.

```python
logo_url  = get_s3().public_url(brand.logo_key)  if brand.logo_key  else None
cover_url = get_s3().public_url(brand.cover_key) if brand.cover_key else None
```

`routes/_product_mapping.py` 의 `build_image_url` 헬퍼 재사용 가능.

### 인증 deps

discover 스펙과 동일 인터페이스.

- `OptionalUser` — Index / Detail / Products
- `CurrentUser` — Follow / Unfollow

### Follow event 정책

| 엔드포인트 | 이벤트 | 조건 |
|-----------|--------|------|
| `POST /brands/{slug}/follow` | `follow` | dual-write (신규 INSERT 시만) |
| `DELETE /brands/{slug}/follow` | `unfollow` | dual-write (실제 DELETE 시만) |

`brand_event.user_id NOT NULL` 제약은 wishlist dual-write와 동일 — 게스트는 애초에 엔드포인트 401.
Brand **view / click** 이벤트(이 스펙에서는 `brand_event_type` enum에는 있으나)는 이번 스콥 out-of-scope — 향후 `POST /events` batched feature 에서 다룸.

### Route 구조

```
app/api/routes/
├── auth.py         (기존)
├── discover.py     (기존)
├── wishlist.py     (기존)
└── brand.py        (신규) — GET /brands, /brands/{slug}, /brands/{slug}/products,
                             POST/DELETE /brands/{slug}/follow
```

`main.py` 에 `app.include_router(brand_routes.router)` 추가 필요.

### Service 구조

```
app/services/
├── auth.py         (기존)
├── product.py      (기존)
├── wishlist.py     (기존)
└── brand_follow.py (신규) — add, remove. slug lookup + dual-write. advisory lock 없음.
```

Index / Detail / Products 의 SELECT는 discover 처럼 route 핸들러에서 `select()` 로 직접 처리(aggregate · cursor 분기가 집중되어 있어 service 추출 이득 적음). Mutation(POST/DELETE)은 service 로 분리.

### `_brand_mapping` 헬퍼

`_product_mapping.py` 와 같은 성격의 `app/api/routes/_brand_mapping.py` 신규 — Brand ORM + aggregate row → `BrandIndexItem` DTO 변환 함수 제공. Related embed · Detail 변환에서 공용.

```python
# app/api/routes/_brand_mapping.py

def to_brand_index_item(
    brand: Brand,
    *,
    product_count: int,
    new_count: int,
    is_followed: bool = False,
) -> BrandIndexItem:
    return BrandIndexItem(
        id=brand.id,
        slug=brand.slug,
        name=brand.name,
        name_ko=brand.name_ko,
        logo_url=build_image_url(brand.logo_key),
        cover_url=build_image_url(brand.cover_key),
        platform=brand.platform,
        styles=list(brand.styles or []),
        is_featured=brand.is_featured,
        is_followed=is_followed,
        product_count=product_count,
        new_count=new_count,
        product_added=brand.product_added,
    )


async def fetch_followed_brand_ids(
    db: AsyncSession,
    user_id: UUID | None,
    brand_ids: list[UUID],
) -> set[UUID]:
    if user_id is None or not brand_ids:
        return set()
    stmt = select(BrandFollow.brand_id).where(
        BrandFollow.user_id == user_id,
        BrandFollow.brand_id.in_(brand_ids),
    )
    rows = (await db.execute(stmt)).all()
    return {r[0] for r in rows}
```

### Logging

- `logging.getLogger("gyeol.brand")`
- 이벤트: `brand.index.ok`, `brand.detail.ok`, `brand.products.ok`, `brand.follow.ok|duplicate`, `brand.unfollow.ok|noop`.
- 에러는 `logger.exception`.

### Transaction Boundaries

| 엔드포인트 | 트랜잭션 | Advisory lock | 이유 |
|-----------|----------|---------------|------|
| `GET /brands` | autocommit (read) | — | |
| `GET /brands/{slug}` | autocommit (read) | — | |
| `GET /brands/{slug}/products` | autocommit (read) | — | |
| `POST /brands/{slug}/follow` | 단일 tx, `async with db.begin()` | **없음** | cap 없음 → race condition 없음. `ON CONFLICT DO NOTHING` + `RETURNING` 로 no-op 판정 |
| `DELETE /brands/{slug}/follow` | 단일 tx, `async with db.begin()` | **없음** | 단일 row DELETE, 합계 검사 없음 |

Wishlist add(advisory lock 사용)와의 차이 — Wishlist는 500 cap이 있어 "insert 전 count 검증 → insert" 사이 race를 막아야 하지만, Brand follow는 cap이 없으므로 `ON CONFLICT DO NOTHING` 으로 race-safe (PostgreSQL row-level lock이 해결). Follower count는 응답용 post-insert SELECT 뿐이라 consistency 요구도 약함.

### Error Enum (AppErrorCodeEnum)

본 스펙에서 사용하는 코드:

| Code | HTTP | 기존 여부 |
|------|------|-----------|
| `VALIDATION_ERROR` | 422 | 기존 |
| `AUTH_REQUIRED` | 401 | 기존 |
| `ACCOUNT_DEACTIVATED` | 403 | 기존 |
| `BRAND_NOT_FOUND` | 404 | **신규 — 추가 필요** |

`app/core/errors.py` 의 `AppErrorCodeEnum` 과 `_HTTP_STATUS` 매핑에 `BRAND_NOT_FOUND = "BRAND_NOT_FOUND"` (HTTP 404) 1건 추가.

---

## Schema Alignment

`data/migrations/001_init.up.sql` + `007_discover_support.up.sql` + `008_public_product_refine.up.sql` + `009_public_brand_platform.up.sql` 기준.

| 스펙 사용 | 스키마 | 비고 |
|-----------|--------|------|
| `brand.{id, slug, name, name_ko, description, logo_key, cover_key, platform, styles, website_url, is_featured, is_active, product_added, created, updated}` | ✓ | 001 + 009 결합 |
| `brand_follow.{user_id, brand_id, created}` PK `(user_id, brand_id)` | ✓ | 001 |
| `brand_event.{id, user_id, type, brand_id, properties, created}`, `brand_event_type` enum (`view/click/follow/unfollow`) | ✓ | 001 — TimescaleDB hypertable |
| `idx_brand_styles` (gin), `idx_brand_featured`, `idx_brand_product_added`, `idx_brand_platform` | ✓ | 001 + 009 |
| `idx_brand_follow_brand (brand_id)` | ✓ | follower_count 집계 가속 |
| `product.{brand_id, popularity, category, is_active}` + `idx_product_brand`, `idx_product_popularity`, `idx_product_category` | ✓ | 001 + 007 |

**ORM 모델 gap: 1건.**

- `BrandEvent` ORM 모델 **없음** — `app/models/brand_event.py` 신규 작성 필요. `ProductEvent` 와 동일 패턴(hypertable, `id insert_default=uuid7_std`, `user_id NOT NULL`, `type` enum = `brand_event_type`, `brand_id` + `properties` + `created`).

```python
# app/models/brand_event.py (신규)
class BrandEventType(StrEnum):
    VIEW = "view"
    CLICK = "click"
    FOLLOW = "follow"
    UNFOLLOW = "unfollow"


class BrandEvent(DeclarativeRoot):
    __tablename__ = "brand_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), init=False, primary_key=True, insert_default=uuid7_std,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[BrandEventType] = mapped_column(
        Enum(
            BrandEventType,
            name="brand_event_type",
            native_enum=True,
            create_type=False,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default_factory=dict,
    )
    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), init=False, server_default=func.now(), nullable=False,
    )
```

`app/models/__init__.py` 에 `BrandEvent`, `BrandEventType` export 추가.

**DB schema gaps: 0건.** 마이그레이션 신규 없음.

---

## Out of Scope

1. Brand view / click event (`POST /events` batched feature 에서)
2. Brand 관리자 CRUD (크롤러·staging → public 승격 파이프라인이 담당)
3. Follow 알림 / 피드 newness 배지 DB (Following 섹션은 discover 에서 별개 처리)
4. Related brand 알고리즘 고도화(embedding, click signal 반영) — 현재는 styles 교집합 + product_added 순
5. Brand search / autocomplete — search feature
6. Cover image 업로드·리사이즈 — S3 크롤러 파이프라인이 채움
7. Follow history / timeline
8. Featured brand editorial override (is_featured 플래그만 존재, 수동 큐레이션 인터페이스 없음)
9. Rate limiting / ETag

---

## Decision Log

| # | Decision | Why |
|---|----------|-----|
| 1 | Brand Index / Detail / Related 모두 동일 `BrandIndexItem` shape 재사용 | FE 카드 컴포넌트 1종으로 처리 |
| 2 | `BrandIndexItem` 는 `BrandSummary`/`BrandFull` 와 별도(상속 X) | 필드 의미가 다르다(`is_followed`는 공유지만 aggregate/cover/is_featured 는 Brand feature 고유) |
| 3 | Follow cap 없음 → advisory lock 없음 | 유저당 follow 수 상한 미지정, 단순 idempotent upsert 로 충분 |
| 4 | Follow dual-write: `brand_follow` + `brand_event` | Wishlist 패턴 계승, 이벤트 싱크는 `POST /events` 없이도 확보 |
| 5 | Index sort cursor 는 sort별 키 다름 → `meta.next` dict 유지 | discover 와 같은 투명 cursor 규약, 재서명/서명키 폐기 |
| 6 | featured sort 는 2-phase(true→false) + 각 phase 내부 `(product_added, id)` keyset | `is_featured DESC, product_added DESC NULLS LAST, id DESC` 삼중 정렬을 단일 row-constructor 로 표현 불가 → bool phase 전환으로 단순화 |
| 7 | recent sort 의 `NULLS LAST` tail 은 2-phase | Postgres 에서 row-constructor 비교 + NULLS LAST 가 깔끔히 안 됨 |
| 8 | Related brands 3 고정, 같은 styles + `product_added DESC NULLS LAST` | 디자인이 "비슷한 결" 3개 강조, 단순 공식이 초기 데이터에서 안정적 |
| 9 | `product_count`/`new_count` 는 LATERAL 서브쿼리로 on-the-fly | 브랜드 수 ≤ 수백, 초기엔 실시간 계산 부담 없음. 스케일 시 summary 테이블 도입 검토 |
| 10 | `DELETE /brands/{slug}/follow` 는 slug 없으면 404 | slug→brand_id 매핑이 실패하면 경로 자체가 무효. Wishlist DELETE 의 "idempotent 500은 완전 허용" 과 다른 이유 — 거긴 product 존재는 이미 path param 이 보장하는 UUID |
| 11 | Follower count 는 응답마다 `SELECT count(*) FROM brand_follow WHERE brand_id = ?` | 초기엔 팔로우 수 100 단위 → 단순 집계 OK. 수만 넘어가면 `brand` 테이블에 캐시 컬럼 도입 |
| 12 | Brand view/click 이벤트는 이번 스펙 out-of-scope | `brand_event_type` enum 에는 정의돼 있지만 FE 가 아직 호출 안 함. `POST /events` 에서 일괄 다룸 |

---

## Checklist

- [x] 5 routes (Index / Detail / Products / Follow / Unfollow)
- [x] Auth · Request · Response · Validation · Data Access · Indexes · Errors · Side Effects 각 섹션
- [x] `meta.next` dict 규약 — alpha / recent / featured / products 각각 명시
- [x] Pydantic DTO — BrandIndexQuery, BrandIndexItem, BrandIndexResponse, BrandDetailResponse, BrandProductsQuery, BrandProductsResponse, BrandFollowResponse
- [x] `BRAND_NOT_FOUND` 404 error enum 추가 명시
- [x] Schema alignment — DB 0 gap, ORM 1 gap (`BrandEvent` 모델 신규)
- [x] Transaction boundaries 명시 + advisory lock 미사용 이유
- [x] Route 등록 (`main.py` `include_router(brand_routes.router)`) 명시
- [x] Related brands 3개 선정 기준(styles `&&` + `product_added DESC NULLS LAST` + `id DESC` + LIMIT 3)
- [x] Out of Scope 9건
- [x] Decision Log 12건
