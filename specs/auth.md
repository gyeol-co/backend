# Spec: auth

디자인 기준: `design/project/Gyeol Unified.html` — `LoginScreen`, `SignupScreen`, `UnifiedNav`.
FE shared contract: `docs/plans/auth/api-mapping.md` (이 스펙과 일치).

## Summary

Gyeol 인증 백엔드 스펙. 이메일/비밀번호 + kakao/naver/google OAuth 3종 지원. 회원가입 시 PIPA 대응을 위한 `consent_log` 4건(terms/privacy/age_14_plus/marketing) 기록 — marketing은 `is_granted=false`여도 기록. 세션은 DB 테이블 없이 `itsdangerous.URLSafeTimedSerializer`로 서명된 HttpOnly 쿠키(`session`). OAuth 신규 유저는 10분 TTL 서명 `pending_token`을 통해 가입 완료. 유저 응답은 `{ data: { user: UserResponse }, meta }` envelope.

## Scope

| # | Endpoint | 기능 |
|---|----------|------|
| 1 | `POST /auth/signup/email` | 이메일+비밀번호 가입 + consent_log 4건 + 세션 쿠키 |
| 2 | `POST /auth/login/email` | 이메일+비밀번호 검증 + 세션 쿠키 |
| 3 | `GET /auth/oauth/{provider}/authorize` | provider authorize URL로 302 + state 쿠키 |
| 4 | `GET /auth/oauth/{provider}/callback` | code 교환 → 기존 유저 로그인 or 신규는 `pending_token` 발급 |
| 5 | `POST /auth/signup/oauth` | OAuth 가입 완료 (pending_token + display_name + consent) |
| 6 | `POST /auth/logout` | 세션 쿠키 삭제 (idempotent, always 200) |
| 7 | `GET /auth/me` | 현재 세션 유저. Guest는 401 `AUTH_REQUIRED` |

---

## 1. `POST /auth/signup/email`

### Auth
비인증. 이미 세션이 있어도 새로 덮어쓴다.

### Request

```json
{
  "email": "you@example.com",
  "password": "at-least-8-chars",
  "display_name": "오기",
  "consents": [
    { "type": "terms",       "is_granted": true,  "policy_version": "v1.0", "method": "signup" },
    { "type": "privacy",     "is_granted": true,  "policy_version": "v1.0", "method": "signup" },
    { "type": "age_14_plus", "is_granted": true,  "policy_version": "v1.0", "method": "signup" },
    { "type": "marketing",   "is_granted": false, "policy_version": "v1.0", "method": "signup" }
  ]
}
```

- `email`: Pydantic `EmailStr`, lower-case 정규화
- `password`: `min_length=8, max_length=128`
- `display_name`: strip 후 `min_length=1, max_length=40`
- `consents`: **정확히 4개** (`min_length=4, max_length=4`). 4개 type 모두 한 번씩 포함. 필수 3종(`terms`, `privacy`, `age_14_plus`)은 `is_granted=true` 필수. `marketing`은 true/false 자유.

### Response 200

```json
{
  "data": {
    "user": {
      "id": "018f...",
      "email": "you@example.com",
      "display_name": "오기",
      "styles": [],
      "has_password": true,
      "oauth_providers": [],
      "visited": null,
      "created": "2026-04-19T05:12:00Z"
    }
  },
  "meta": { "request_id": "req_..." }
}
```

`response_model=StandardSuccessResponse[UserEnvelope]`. `Set-Cookie: session=...`.

### Data Access

단일 트랜잭션:

1. **SELECT** `id` FROM `"user"` WHERE `lower(email) = :email` — 있으면 `EMAIL_ALREADY_EXISTS`.
2. **INSERT** `"user" (email, password_hash, password_changed, display_name, styles, is_active)`:
   - `password_hash` = argon2id(password)
   - `password_changed` = `now()`
   - `styles` = `{}`
3. **INSERT** `consent_log (user_id, type, policy_version, is_granted, method='signup', ip_address, user_agent)` × 4.
   - consents의 4개 entry 전부 기록. `is_granted=false`도 그대로 기록.
   - `ip_address`: `X-Forwarded-For` 첫 토큰 or `request.client.host`
   - `user_agent`: `User-Agent` 헤더 (1000자 truncate)
   - `method`: 요청값 무시, 서버 `'signup'` 고정

커밋 후 세션 쿠키 발급.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | email 형식 오류 / password 8자 미만 / display_name 공백 / consents 4개 아님 / type 중복 |
| `CONSENT_REQUIRED` | 400 | 필수 3종 중 하나라도 `is_granted=true`가 아님 |
| `EMAIL_ALREADY_EXISTS` | 409 | `lower(email)` 이미 `"user"` 테이블에 존재 |

### Edge Cases & Side Effects

- 동시 가입 race: unique 제약 위반(IntegrityError) 잡아 `EMAIL_ALREADY_EXISTS`로 변환.
- `visited`는 업데이트 하지 않음 (login/callback에서만).
- Side effects: `"user"` 1 row, `consent_log` 4 rows, 세션 쿠키.

---

## 2. `POST /auth/login/email`

### Auth
비인증.

### Request

```json
{ "email": "you@example.com", "password": "..." }
```

### Response 200

```json
{
  "data": {
    "user": {
      "id": "018f...",
      "email": "you@example.com",
      "display_name": "오기",
      "styles": ["minimal"],
      "has_password": true,
      "oauth_providers": ["kakao"],
      "visited": "2026-04-18T10:00:00Z",
      "created": "2026-01-01T00:00:00Z"
    }
  },
  "meta": { "request_id": "req_..." }
}
```

`Set-Cookie: session=...`

### Data Access

1. **SELECT** `u.id, u.email, u.password_hash, u.display_name, u.styles, u.is_active, u.visited, u.created,
   array_agg(o.provider ORDER BY o.created) FILTER (WHERE o.id IS NOT NULL) AS oauth_providers`
   FROM `"user" u LEFT JOIN oauth o ON o.user_id = u.id` WHERE `lower(u.email) = :email` GROUP BY u.id.
2. Row 없음 → dummy argon2 verify 후 `INVALID_CREDENTIALS` (user enumeration 방지).
3. `password_hash IS NULL` (OAuth-only 계정) → `INVALID_CREDENTIALS`.
4. `argon2.verify(hash, password)` 실패 → `INVALID_CREDENTIALS`.
5. `is_active=false` → `ACCOUNT_DEACTIVATED`.
6. **UPDATE** `"user" SET visited = now(), updated = now() WHERE id = :id`.
7. `has_password` = `password_hash IS NOT NULL` = `True`. `oauth_providers` from aggregate.

세션 쿠키 발급.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | email 형식 오류 |
| `INVALID_CREDENTIALS` | 401 | email 없음 / password 불일치 / OAuth-only 계정 (password_hash NULL) |
| `ACCOUNT_DEACTIVATED` | 403 | `is_active=false` |

### Edge Cases & Side Effects

- argon2 timing attack 방지: 이메일 없어도 dummy hash verify 1회 후 401.
- `visited`/`updated` 갱신.

---

## 3. `GET /auth/oauth/{provider}/authorize`

### Auth
비인증.

### Request

Path: `provider ∈ {kakao, naver, google}`.
Query: `next: str | None = None` — 성공 후 FE가 돌아갈 path (e.g. `/discover`). `http://`/`https://` 등 절대 URL 거부(open redirect 방지). 서버는 서명 state에 넣어 callback 응답에 포함.

### Response

`HTTP 302 Location: <provider authorize URL>`

`Set-Cookie: oauth_state=<signed>; Path=/auth/oauth; Max-Age=600; HttpOnly; SameSite=Lax; Secure`

Provider별 scope:
- kakao: `account_email profile_nickname`
- naver: `name,email`
- google: `openid email profile`

### Data Access
없음. 환경변수에서 client_id/redirect_uri 로드.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | provider가 셋 중 아님 / next가 absolute URL |

### Side Effects
쿠키 1개 세팅, 302 redirect.

---

## 4. `GET /auth/oauth/{provider}/callback`

### Auth
비인증. `oauth_state` 쿠키 검증.

### Request

Path: `provider`.
Query: `code: str`, `state: str`, `error: str | None` (provider가 거절 시).

### Response 200 (두 가지 분기, discriminator = `status`)

**Case A — 기존 유저 로그인**:
```json
{
  "data": {
    "status": "logged_in",
    "user": {
      "id": "018f...",
      "email": "you@kakao.com",
      "display_name": "오기",
      "styles": [],
      "has_password": false,
      "oauth_providers": ["kakao"],
      "visited": "2026-04-18T10:00:00Z",
      "created": "2025-10-01T00:00:00Z"
    },
    "next": "/discover"
  },
  "meta": { "request_id": "req_..." }
}
```
`Set-Cookie: session=...` + `Set-Cookie: oauth_state=; Max-Age=0`

**Case B — 신규 유저 가입 필요**:
```json
{
  "data": {
    "status": "needs_signup",
    "pending_token": "<signed token, 10min TTL>",
    "provider": "kakao",
    "provider_email": "you@kakao.com",
    "next": "/discover"
  },
  "meta": { "request_id": "req_..." }
}
```
`Set-Cookie: oauth_state=; Max-Age=0` (세션은 아직 발급 안 함)

### Data Access

1. 쿼리 `error` 있음 → `OAUTH_USER_DENIED` (provider에서 유저가 거절/취소).
2. `state` query + `oauth_state` cookie double-check + 서명 + TTL + payload의 provider = path provider → 불일치 시 `OAUTH_STATE_MISMATCH`.
3. Provider token endpoint에 code 교환(httpx async) → 실패 시 `OAUTH_PROVIDER_ERROR`.
4. Provider userinfo endpoint → `provider_uid`, `provider_email` 획득. `provider_email` 비어있으면 `OAUTH_PROVIDER_ERROR`.
5. **SELECT** `user_id` FROM `oauth` WHERE `provider = :p AND provider_uid = :uid`.
   - **있음 (기존 유저)**:
     - **SELECT** `"user"` row + oauth aggregate (login과 동일) → `is_active=false` 면 `ACCOUNT_DEACTIVATED`.
     - **UPDATE** `"user" SET visited = now(), updated = now() WHERE id = :id`.
     - 세션 쿠키 발급 → Case A 반환.
   - **없음 (신규)**:
     - `pending_token` = `URLSafeTimedSerializer(salt="gyeol.oauth.pending").dumps({provider, provider_uid, provider_email, nonce})`. TTL 10분.
     - DB 기록 없음 (stateless).
     - Case B 반환.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | provider 불일치 / code 누락 |
| `OAUTH_USER_DENIED` | 400 | 쿼리 `?error=...` 존재 (provider에서 유저가 거절) |
| `OAUTH_STATE_MISMATCH` | 400 | state 쿠키 없음 / 쿠키-쿼리 state 불일치 / 서명 위조 / TTL 만료 |
| `OAUTH_PROVIDER_ERROR` | 502 | token/userinfo 호출 실패 / 5xx / `provider_email` 빈 값 |
| `ACCOUNT_DEACTIVATED` | 403 | 매칭된 `"user".is_active=false` |

### Edge Cases & Side Effects

- **같은 이메일·다른 provider**: email 가입 계정이 같은 이메일로 OAuth 시도 → `oauth` 테이블 조회만 하므로 매칭 안 됨 → Case B(신규). signup/oauth 단계에서 `EMAIL_ALREADY_EXISTS` 반환. Account linking은 out-of-scope.
- naver는 `email_verified` 개념 약함 → 그대로 사용.
- `oauth(provider, provider_uid)` UNIQUE가 중복 방지 안전망.
- Side effects: 기존 유저면 `"user".visited/updated` + 세션 쿠키. 신규면 DB 변경 없음. 항상 `oauth_state` 쿠키 제거.

---

## 5. `POST /auth/signup/oauth`

### Auth
비인증. `pending_token` 필수.

### Request

```json
{
  "pending_token": "<from /callback>",
  "display_name": "오기",
  "consents": [
    { "type": "terms",       "is_granted": true,  "policy_version": "v1.0", "method": "signup" },
    { "type": "privacy",     "is_granted": true,  "policy_version": "v1.0", "method": "signup" },
    { "type": "age_14_plus", "is_granted": true,  "policy_version": "v1.0", "method": "signup" },
    { "type": "marketing",   "is_granted": false, "policy_version": "v1.0", "method": "signup" }
  ]
}
```

- `pending_token`: 서명/TTL 유효
- `display_name`: `min_length=1, max_length=40`
- `consents`: signup/email과 동일 규칙 (정확히 4개, 필수 3종 granted)

### Response 200

```json
{
  "data": {
    "user": {
      "id": "018f...",
      "email": "you@kakao.com",
      "display_name": "오기",
      "styles": [],
      "has_password": false,
      "oauth_providers": ["kakao"],
      "visited": null,
      "created": "2026-04-19T05:12:00Z"
    }
  },
  "meta": { "request_id": "req_..." }
}
```

`Set-Cookie: session=...`

### Data Access

1. `pending_token` unsign:
   - 서명 위조/파싱 실패 → `PENDING_TOKEN_INVALID` (400)
   - TTL 만료 → `PENDING_TOKEN_EXPIRED` (401)
   - payload에서 `provider`, `provider_uid`, `provider_email` 꺼냄.
2. 단일 트랜잭션:
   a. **SELECT** FROM `oauth` WHERE `provider=:p AND provider_uid=:uid` — 있으면 `OAUTH_ALREADY_LINKED`.
   b. **SELECT** FROM `"user"` WHERE `lower(email)=:provider_email` — 있으면 `EMAIL_ALREADY_EXISTS` (account linking 없음).
   c. **INSERT** `"user" (email=:provider_email, password_hash=NULL, password_changed=NULL, display_name, styles='{}', is_active=true)`.
   d. **INSERT** `oauth (user_id, provider, provider_uid, provider_email)`.
   e. **INSERT** `consent_log` × 4 (`method='signup'` 고정, IP/UA 캡처).

커밋 후 세션 쿠키 발급. Response의 `has_password=false`, `oauth_providers=[provider]`.

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `VALIDATION_ERROR` | 422 | display_name 공백 / consents 4개 아님 |
| `CONSENT_REQUIRED` | 400 | 필수 3종 granted 아님 |
| `PENDING_TOKEN_INVALID` | 400 | 서명 위조 / 파싱 실패 |
| `PENDING_TOKEN_EXPIRED` | 401 | TTL 만료 (10분 초과) |
| `OAUTH_ALREADY_LINKED` | 409 | `oauth(provider, provider_uid)` 이미 존재 (중복 요청 / 콜백과 경쟁) |
| `EMAIL_ALREADY_EXISTS` | 409 | `"user".email`(provider_email과 같은 값)이 이미 존재 — account linking 없음 |

### Edge Cases & Side Effects

- 같은 `pending_token` 재사용: 두 번째 요청은 `oauth` unique 제약으로 `OAUTH_ALREADY_LINKED`. FE 버튼 disable로 UX 보완.
- Side effects: `"user"` + `oauth` + `consent_log` ×4 INSERT, 세션 쿠키.

---

## 6. `POST /auth/logout`

### Auth
비인증 (idempotent). 쿠키 있든 없든 200.

### Request
Body 없음.

### Response 200

```json
{
  "data": { "logged_out": true },
  "meta": { "request_id": "req_..." }
}
```

`Set-Cookie: session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure`

### Data Access
없음.

### Error Cases
없음. 항상 200.

### Side Effects
세션 쿠키 만료.

---

## 7. `GET /auth/me`

### Auth
필수. 쿠키 없음/invalid → 401 `AUTH_REQUIRED`.

### Request
쿠키 `session` 자동 파싱.

### Response 200

```json
{
  "data": {
    "user": {
      "id": "018f...",
      "email": "you@example.com",
      "display_name": "오기",
      "styles": ["minimal"],
      "has_password": true,
      "oauth_providers": ["google"],
      "visited": "2026-04-18T10:00:00Z",
      "created": "2026-01-01T00:00:00Z"
    }
  },
  "meta": { "request_id": "req_..." }
}
```

### Data Access

1. 쿠키 unsign → `user_id`. 실패(서명/TTL) → `AUTH_REQUIRED`.
2. **SELECT** login과 동일한 user+oauth aggregate 쿼리 by `id = :user_id`.
3. Row 없음 (유저 삭제됨) → `AUTH_REQUIRED`.
4. `is_active=false` → `ACCOUNT_DEACTIVATED`.

세션 renewal은 본 스펙 out-of-scope (30일 고정 만료).

### Error Cases

| Code | HTTP | When |
|------|------|------|
| `AUTH_REQUIRED` | 401 | 쿠키 없음 / 서명 위조 / 만료 / 가리키는 user 없음 |
| `ACCOUNT_DEACTIVATED` | 403 | `is_active=false` |

### Side Effects
없음 (순수 read). `visited`는 여기서 갱신하지 않음.

---

## Session Design

### Cookie

| 속성 | 값 |
|------|----|
| Name | `session` |
| Value | `URLSafeTimedSerializer(SECRET, salt="gyeol.session").dumps({"user_id": str, "issued_at": int})` |
| Path | `/` |
| Max-Age | 2,592,000s (30일) |
| HttpOnly | true |
| Secure | true (prod) / false (local) |
| SameSite | Lax |

### OAuth tokens

| 이름 | salt | TTL |
|------|------|-----|
| `oauth_state` (쿠키) | `"gyeol.oauth.state"` | 600s |
| `pending_token` (응답 body) | `"gyeol.oauth.pending"` | 600s |

### Secret
환경변수 `SESSION_SECRET` (32바이트 random). 회전 시 기존 세션 무효.

### Renewal
고정 30일 만료. Sliding session은 out-of-scope.

---

## Pydantic DTOs

```python
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


ConsentType = Literal["terms", "privacy", "marketing", "age_14_plus"]
ConsentMethod = Literal["signup", "settings", "re_consent"]
OAuthProvider = Literal["kakao", "naver", "google"]


class ConsentEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ConsentType
    policy_version: str = Field(min_length=1, max_length=32)
    is_granted: bool
    method: ConsentMethod  # 서버는 항상 'signup'으로 강제


class EmailSignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=40)
    consents: list[ConsentEntryInput] = Field(min_length=4, max_length=4)


class EmailLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class OAuthSignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending_token: str = Field(min_length=1, max_length=2048)
    display_name: str = Field(min_length=1, max_length=40)
    consents: list[ConsentEntryInput] = Field(min_length=4, max_length=4)


class UserResponse(BaseModel):
    """사용자 공개 표현. `has_password`/`oauth_providers`는 쿼리로 계산."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str
    styles: list[str]
    has_password: bool          # password_hash IS NOT NULL
    oauth_providers: list[OAuthProvider]  # 연결된 provider
    visited: datetime | None
    created: datetime


class UserEnvelope(BaseModel):
    """로그인/가입/me 응답의 `data` 본체."""
    user: UserResponse


class OAuthCallbackLoggedIn(BaseModel):
    status: Literal["logged_in"] = "logged_in"
    user: UserResponse
    next: str


class OAuthCallbackNeedsSignup(BaseModel):
    status: Literal["needs_signup"] = "needs_signup"
    pending_token: str
    provider: OAuthProvider
    provider_email: EmailStr
    next: str


class OAuthCallbackResponse(BaseModel):
    """Pydantic v2 discriminated union으로 `status` 필드 기준 분기."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["logged_in", "needs_signup"]
    # logged_in only
    user: UserResponse | None = None
    # needs_signup only
    pending_token: str | None = None
    provider: OAuthProvider | None = None
    provider_email: EmailStr | None = None
    # always
    next: str


class LogoutResponse(BaseModel):
    logged_out: Literal[True] = True
```

---

## Shared Concerns

### Password Hashing

- `argon2-cffi`: `PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)`.
- 로그인 이메일 미존재 케이스에도 dummy hash 1회 verify → timing-leak 완화.

### IP/UA 캡처

```python
def extract_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else
            (request.client.host if request.client else None))

def extract_ua(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:1000] if ua else None
```

signup/email, signup/oauth에서 `consent_log` 4 rows에 공통 적용.

### CSRF

- POST 엔드포인트: JSON + `SameSite=Lax` 쿠키.
- OAuth: `state` + `oauth_state` 쿠키 double-submit.

### Error Enum (AppErrorCodeEnum)

본 스펙 범위 내에서 새로 필요한 / 확인 필요한 코드:

| Code | HTTP |
|------|------|
| `VALIDATION_ERROR` | 422 |
| `AUTH_REQUIRED` | 401 |
| `INVALID_CREDENTIALS` | 401 |
| `ACCOUNT_DEACTIVATED` | 403 |
| `CONSENT_REQUIRED` | 400 |
| `EMAIL_ALREADY_EXISTS` | 409 |
| `OAUTH_ALREADY_LINKED` | 409 |
| `OAUTH_STATE_MISMATCH` | 400 |
| `OAUTH_USER_DENIED` | 400 |
| `OAUTH_PROVIDER_ERROR` | 502 |
| `PENDING_TOKEN_INVALID` | 400 |
| `PENDING_TOKEN_EXPIRED` | 401 |

### Dependency Injection

- `DbSession = Annotated[AsyncSession, Depends(get_db_session)]`
- `CurrentUser = Annotated[User, Depends(require_auth)]` — `/auth/me` 용

### Rate Limiting (out of spec — 권장값만)

- `POST /auth/login/email`: 10/분/IP
- `POST /auth/signup/*`: 5/시간/IP
- `GET /auth/oauth/*/callback`: 20/분/IP

### Logging

- `logging.getLogger("gyeol.auth")`.
- 이벤트: `auth.signup.email.ok|fail`, `auth.login.email.ok|fail`, `auth.oauth.callback.{logged_in|needs_signup|fail}`, `auth.logout`.
- 실패 로그에 이메일 원문/비밀번호 해시 저장 금지.

---

## Schema Alignment

`gyeol/data/migrations/001_init.up.sql` 기준:

| 스펙 사용 | 스키마 존재 | 비고 |
|-----------|-------------|------|
| `"user".{id, email, password_hash, password_changed, display_name, styles, is_active, visited, created, updated}` | ✓ 전부 | `email UNIQUE` (case-sensitive — 앱에서 `lower()` 정규화) |
| `oauth.{id, user_id, provider, provider_uid, provider_email, created}` | ✓ 전부 | `UNIQUE(provider, provider_uid)` |
| `consent_log.{id, user_id, type, policy_version, is_granted, method, ip_address, user_agent, created}` | ✓ 전부 | append-only |
| ENUM `consent_type` / `consent_method` | ✓ | |

**Schema gaps: 0건.** 신규 테이블 불필요.

UserResponse의 파생 필드:
- `has_password` = `password_hash IS NOT NULL` — 쿼리 내 CASE 또는 Python 레벨 계산
- `oauth_providers` = `array_agg(oauth.provider ORDER BY oauth.created)` — LEFT JOIN + GROUP BY

---

## Out of Scope

1. Password reset / forgot-password
2. Email verification
3. Re-consent / consent-settings (`method in ('settings','re_consent')`)
4. Account linking (email 계정 ↔ OAuth 계정 머지)
5. Admin role / user 관리 API
6. Rate limiting middleware 구현
7. Sliding session renewal
8. CSRF token 별도 미들웨어
9. 2FA
10. 탈퇴 / 비활성화 API
11. OAuth token refresh / provider access_token 저장

---

## Checklist

- [x] 7 endpoints 스펙: method / path / auth / request / response envelope
- [x] Data access: SELECT/INSERT/UPDATE + 트랜잭션 경계
- [x] Error codes: auth 도메인 구체 이름 (12 codes)
- [x] Edge cases: 중복 가입 / OAuth 재콜백 / pending_token 재사용 / state 위조
- [x] Schema alignment: 0 gaps
- [x] FE shared contract 일치 (`docs/plans/auth/api-mapping.md`)
