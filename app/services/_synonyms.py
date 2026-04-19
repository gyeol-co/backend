"""Fashion-domain synonym expansion for FTS query construction.

Loads `data/common/synonyms.yaml` at process start and builds a `str -> set[str]`
map so each MeCab token can be expanded to its known variants when we assemble
a `to_tsquery` OR-group.

MeCab is morphological, not synonymous — `자켓 / 재킷` survive as distinct
lexemes. This module supplies the missing layer. Keep the yaml small and
curated; every addition shows up as extra tsquery width.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger("gyeol.search.synonyms")


def _resolve_synonyms_path() -> Path:
    """Find `synonyms.yaml` in whichever layout the process lives in.

    - Dev: repo-root relative
      (`app/backend/app/services/_synonyms.py` → `../../../../data/common/…`).
    - Docker: bundled at `/app/data/common/synonyms.yaml` by the image build.
    - Override: `SYNONYMS_PATH` env var always wins.
    """
    override = os.environ.get("SYNONYMS_PATH")
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    candidates: list[Path] = [
        # Dev — climb 4 levels to the repo root if deep enough.
        *([here.parents[4] / "data" / "common" / "synonyms.yaml"] if len(here.parents) > 4 else []),
        # Docker — image copies the file here (see app/backend/Dockerfile).
        Path("/app/data/common/synonyms.yaml"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    # Return the first candidate anyway so `_load_map` surfaces a consistent
    # "not found" warning with its absolute path.
    return candidates[0] if candidates else Path("data/common/synonyms.yaml")


_SYNONYMS_PATH = _resolve_synonyms_path()


def _normalize(token: str) -> str:
    return token.strip().lower()


@lru_cache(maxsize=1)
def _load_map() -> dict[str, frozenset[str]]:
    """Return `token → frozenset(synonyms_including_self)`. Empty on any error
    (fail-open — missing file degrades to no expansion)."""
    try:
        with _SYNONYMS_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("synonyms.not_found path=%s", _SYNONYMS_PATH)
        return {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning("synonyms.load_failed path=%s err=%s", _SYNONYMS_PATH, e)
        return {}

    if not isinstance(data, list):
        logger.warning("synonyms.bad_shape type=%s", type(data).__name__)
        return {}

    expand: dict[str, set[str]] = {}
    for row in data:
        if not isinstance(row, list):
            continue
        variants = {_normalize(str(v)) for v in row if str(v).strip()}
        if len(variants) <= 1:
            continue
        for v in variants:
            expand.setdefault(v, set()).update(variants)

    frozen = {k: frozenset(vs) for k, vs in expand.items()}
    logger.info("synonyms.loaded groups=%d tokens=%d", len(data), len(frozen))
    return frozen


def expand_token(token: str) -> list[str]:
    """Return `[token, *synonyms]` for the given lexeme (case-insensitive
    lookup, output preserves lower-case). When no synonym row matches, returns
    a single-element list with the normalised token.
    """
    norm = _normalize(token)
    if not norm:
        return []
    variants = _load_map().get(norm)
    if variants is None:
        return [norm]
    # Put the original token first so downstream code can treat it as primary.
    rest = [v for v in variants if v != norm]
    return [norm, *sorted(rest)]


def warmup() -> None:
    """Eager yaml load — called from FastAPI lifespan. Idempotent."""
    _load_map()


__all__ = ["expand_token", "warmup"]
