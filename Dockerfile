# syntax=docker/dockerfile:1.7
#
# Build context = repo root (see deploy/docker-compose.yml). This lets the
# image pick up `data/common/*.yaml` alongside the backend source.

# ---- builder: install deps into /app/.venv with uv ----
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dep cache layer — invalidates only on pyproject/lock change.
COPY app/backend/pyproject.toml app/backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# App source.
COPY app/backend/app ./app
RUN uv sync --frozen --no-dev


# ---- runtime ----
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd -r app && useradd -r -g app -u 1000 -d /app -s /sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app  /app/app

# Shared taxonomy / synonyms — BE consumes `data/common/synonyms.yaml`.
COPY --chown=app:app data/common /app/data/common

USER app
EXPOSE 8000

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
