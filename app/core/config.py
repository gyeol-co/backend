from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: str = "local"

    DATABASE_URL: str = "postgresql+asyncpg://gyeol:gyeolpass@localhost:5432/gyeol"
    REDIS_URL: str = "redis://:redispass@localhost:6379/0"

    SESSION_SECRET: str = "dev-secret-change-me"

    COOKIE_SECURE: bool = False
    CORS_ORIGINS: str = "http://localhost:3000"

    # AWS S3 — product images (bucket: `gyeol`, ap-southeast-2)
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "ap-southeast-2"
    S3_BUCKET: str = "gyeol"
    S3_PUBLIC_BASE_URL: str = "https://gyeol.s3.ap-southeast-2.amazonaws.com"

    KAKAO_CLIENT_ID: str = ""
    KAKAO_CLIENT_SECRET: str = ""
    KAKAO_REDIRECT_URI: str = "http://localhost:8000/auth/oauth/kakao/callback"

    NAVER_CLIENT_ID: str = ""
    NAVER_CLIENT_SECRET: str = ""
    NAVER_REDIRECT_URI: str = "http://localhost:8000/auth/oauth/naver/callback"

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/oauth/google/callback"

    SESSION_COOKIE_NAME: str = "session"
    SESSION_MAX_AGE: int = 60 * 60 * 24 * 30  # 30 days
    # Cookie domain — `.gyeol.co` in prod so the session cookie is shared
    # between `gyeol.co` (FE) and `api.gyeol.co` (BE). Leave unset (`None`)
    # in dev — browser scopes to the issuing host.
    COOKIE_DOMAIN: str | None = None

    OAUTH_STATE_COOKIE_NAME: str = "oauth_state"
    OAUTH_STATE_MAX_AGE: int = 600
    PENDING_TOKEN_MAX_AGE: int = 600

    # Search — TEI services (docker-compose). Reranker is used on the
    # embedding-only fallback path; disabled wholesale if unreachable.
    EMBEDDING_URL: str = "http://localhost:8080"
    RERANKER_URL: str = "http://localhost:8081"

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
