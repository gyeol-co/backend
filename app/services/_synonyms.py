"""MeCab keeps spellings such as 자켓/재킷 distinct; expand them before FTS."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger("gyeol.search.synonyms")


def _resolve_synonyms_path() -> Path:
    override = os.environ.get("SYNONYMS_PATH")
    if override:
        return Path(override)

    return Path(__file__).resolve().parents[1] / "resources" / "synonyms.yaml"


_SYNONYMS_PATH = _resolve_synonyms_path()


def _normalize(token: str) -> str:
    return token.strip().lower()


@lru_cache(maxsize=1)
def _load_map() -> dict[str, frozenset[str]]:
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

    synonyms_by_token: dict[str, set[str]] = {}
    for row in data:
        if not isinstance(row, list):
            continue
        variants = {_normalize(str(v)) for v in row if str(v).strip()}
        if len(variants) <= 1:
            continue
        for v in variants:
            synonyms_by_token.setdefault(v, set()).update(variants)

    frozen = {token: frozenset(variants) for token, variants in synonyms_by_token.items()}
    logger.info("synonyms.loaded groups=%d tokens=%d", len(data), len(frozen))
    return frozen


def expand_token(token: str) -> list[str]:
    """Return the normalized token first, followed by its synonyms in sorted order."""
    norm = _normalize(token)
    if not norm:
        return []
    variants = _load_map().get(norm)
    if variants is None:
        return [norm]
    rest = [v for v in variants if v != norm]
    return [norm, *sorted(rest)]


def warmup() -> None:
    _load_map()


__all__ = ["expand_token", "warmup"]
