# gyeol-backend

결의 상품 탐색·검색·브랜드·위시리스트·인증 API다.
Python 3.12, FastAPI, SQLAlchemy async, asyncpg를 사용한다.
Postgres는 pgvector·TimescaleDB를 사용하고, 세션 관련 상태는 Redis에 저장한다.
상품 이미지는 S3, 검색 임베딩과 리랭킹은 TEI에 의존한다.

## 로컬 개발

Postgres와 Redis가 실행 중이어야 한다. 호스트 서비스 설정은
[deployments 저장소](https://github.com/gyeol-co/deployments)에서 관리한다.
TEI의 embedding·reranker 서비스 정의는 data 저장소의 `docker-compose.yml`에 있다.

```bash
cp .env.example .env
uv sync --frozen
uv run uvicorn app.main:app --reload --port 8000
```

실행 전에 `.env`의 DB·Redis 접속 정보와 `SESSION_SECRET`을 채운다.
OAuth·S3를 사용할 때는 해당 자격증명도 설정한다.
전체 환경변수와 기본값은 [Settings](app/core/config.py), 로컬 예시는 [.env.example](.env.example)을 따른다.

## 코드 구조

| 위치 | 책임 |
|---|---|
| [app/main.py](app/main.py) | 앱 생성·생명주기·미들웨어·예외 처리·상태 확인 |
| `app/api/` | HTTP 라우터와 DB 세션·인증 의존성 |
| `app/services/` | 인증·검색·상품·위시리스트 등의 처리 |
| `app/clients/` | TEI embedding·reranker 호출 |
| `app/core/` | 설정·DB·Redis·S3·세션·공통 응답 |
| `app/models/`, `app/schemas/` | SQLAlchemy 모델과 요청·응답 타입 |
| `app/oauth_providers/` | Kakao·Naver·Google OAuth 연동 |
| `app/resources/synonyms.yaml` | 이미지에 함께 포함되는 검색 동의어 사전 |

### 요청 처리

DB 세션을 사용하는 API는 [get_db_session](app/core/db.py)의 트랜잭션을 공유한다.
요청이 정상 종료되면 commit하고, 예외가 발생하면 rollback한다.
서비스에서 별도 `db.begin()`을 호출하지 않는다.

업무 API는 [공통 응답 타입](app/core/envelope.py)의 `data`와 `meta`를 반환한다.
`meta`에는 request ID, 다음 페이지 커서, 오류 코드가 들어간다.
`AppException`과 요청 검증 오류는 `main.py`의 예외 처리기에서 변환한다.
상태 확인 엔드포인트는 별도의 간단한 JSON 응답을 사용한다.

### 검색

[검색 서비스](app/services/search.py)는 MeCab 토큰에 동의어를 확장해 Postgres FTS로 후보를 찾는다.
관련도 정렬에서는 임베딩 유사도와 카테고리 의도를 점수에 반영한다.
FTS 후보가 없으면 임베딩 ANN 검색을 시도하고, 이 경로에서 cross-encoder 리랭커를 사용한다.
이후 커서를 적용하고 위시리스트 등 사용자별 정보를 보강한다.

TEI 호출 실패 시 FTS 결과는 키워드 점수 순서를 사용하고, 리랭커 실패 시 ANN 순서를 유지한다.
다른 정렬 옵션은 SQL 정렬을 사용한다.

동의어 사전은 기본적으로 [app/resources/synonyms.yaml](app/resources/synonyms.yaml)을 읽는다.
`SYNONYMS_PATH`로 다른 파일을 지정할 수 있다. MeCab이 실제로 반환하는 단일 토큰을 등록한다.

## 상태 확인

| 경로 | 검사 | 실패 시 |
|---|---|---|
| `/healthz` | 프로세스 응답 | Kubernetes liveness/startup 실패 판단 |
| `/readyz` | 3초 안에 Postgres `SELECT 1`과 Redis `PING` 완료 | HTTP 503으로 트래픽에서 제외 |

DB·Redis 장애가 프로세스 재시작으로 이어지지 않도록 두 검사를 분리한다.
TEI는 검색의 선택적 의존성이므로 readiness 검사에 포함하지 않는다.

## 이미지와 배포

이 저장소를 빌드 컨텍스트로 사용한다. 형제 저장소 없이 빌드할 수 있다.

```bash
docker build -t gyeol-backend:local .
```

[GitHub Actions](.github/workflows/build-push.yml)는 테스트와 이미지 빌드를 수행한다.
PR 이외의 실행에서는 `ghcr.io/gyeol-co/backend:sha-<commit>`을 게시하고 digest를 출력한다.
배포할 digest는 deployments 저장소의 Helm values에 기록한다.

실행 명령·Pod 개수·자원 제한·환경 설정·Argo CD Application은
[deployments 저장소](https://github.com/gyeol-co/deployments)에서 관리한다.

## 테스트

```bash
uv run --frozen pytest
```
