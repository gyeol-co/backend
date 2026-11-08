from __future__ import annotations

import asyncio
import logging
import logging.config
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text

from app.api.routes import auth as auth_routes
from app.api.routes import brand as brand_routes
from app.api.routes import discover as discover_routes
from app.api.routes import search as search_routes
from app.api.routes import wishlist as wishlist_routes
from app.clients import embedding as embedding_client
from app.clients import reranker as reranker_client
from app.core.config import get_settings
from app.core.db import get_engine
from app.core.envelope import ErrorMeta, StandardErrorResponse, StandardResponseMeta
from app.core.errors import AppErrorCodeEnum, AppException
from app.core.redis import close_redis, get_redis
from app.core.request_id import RequestIdMiddleware, current_request_id
from app.services import _synonyms as synonym_loader
from app.services import _tokenizer as tokenizer

logger = logging.getLogger("gyeol")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Avoid loading dictionaries on the first search request.
    tokenizer.warmup()
    synonym_loader.warmup()
    yield
    await close_redis()
    await embedding_client.close_client()
    await reranker_client.close_client()


def create_app() -> FastAPI:
    _configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="gyeol-backend",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        code = exc.error_code
        status = getattr(code, "http_status", 500)
        body = StandardErrorResponse(
            meta=StandardResponseMeta(
                request_id=current_request_id(),
                error=ErrorMeta(code=code.value, detail_payload=exc.detail_payload),
            ),
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Validation context may contain exceptions that JSON cannot serialize.
        errors = [
            {k: v for k, v in err.items() if k not in ("ctx", "url")}
            for err in exc.errors()
        ]
        body = StandardErrorResponse(
            meta=StandardResponseMeta(
                request_id=current_request_id(),
                error=ErrorMeta(
                    code=AppErrorCodeEnum.VALIDATION_ERROR.value,
                    detail_payload={"errors": errors},
                ),
            ),
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(ValidationError)
    async def _pydantic_validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
        # Depends model validators can bypass RequestValidationError.
        # Exclude exception-valued context from the JSON response.
        errors = exc.errors(include_url=False, include_context=False)
        body = StandardErrorResponse(
            meta=StandardResponseMeta(
                request_id=current_request_id(),
                error=ErrorMeta(
                    code=AppErrorCodeEnum.VALIDATION_ERROR.value,
                    detail_payload={"errors": errors},
                ),
            ),
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception path=%s err=%s\n%s",
            request.url.path,
            exc,
            traceback.format_exc(),
        )
        body = StandardErrorResponse(
            meta=StandardResponseMeta(
                request_id=current_request_id(),
                error=ErrorMeta(code=AppErrorCodeEnum.INTERNAL_ERROR.value),
            ),
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    app.include_router(auth_routes.router)
    app.include_router(brand_routes.router)
    app.include_router(discover_routes.router)
    app.include_router(search_routes.router)
    app.include_router(wishlist_routes.router)

    @app.get("/healthz")
    async def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def _readyz() -> JSONResponse:
        try:
            async with asyncio.timeout(3):
                async with get_engine().connect() as connection:
                    await connection.execute(text("SELECT 1"))
                redis = await get_redis()
                await redis.ping()
        except Exception:
            # Keep dependency outages separate from liveness, and credentials out of logs.
            logger.warning("readiness.dependencies_unavailable")
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(content={"status": "ok"})

    return app


app = create_app()
