.PHONY: format check test

# 자동 수정 (ruff format + import sort)
format:
	uv run ruff format app/ tests/
	uv run ruff check --fix app/ tests/

# CI-safe: 수정 없이 검증만 (포맷 → lint → type)
check:
	uv run ruff format --check app/ tests/
	uv run ruff check app/ tests/
	uv run ty check app/

test:
	uv run pytest tests/
