"""Query-side Korean tokenizer — mirrors `data/etl/gold/tokenize.py`.

Users type natural phrases ("워크자켓", "유니폼 셔츠"); the ETL has already
stored MeCab-tokenised lexemes in `product.search_text`. To make FTS match
symmetric we tokenise the query the same way and hand the joined lexemes to
`plainto_tsquery('simple', ...)`.

POS filter / stop words / single-char Hangul removal duplicate the ETL
exactly — divergences break recall. Keep in sync when the ETL tokenizer
evolves.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Fashion corpus POS filter: 일반명사 / 고유명사 / 외국어 / 숫자.
_KEEP_POS: frozenset[str] = frozenset({"NNG", "NNP", "SL", "SN"})

# Stop words — low-signal in a fashion catalog.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        # 일반 의존명사/추상
        "수", "것", "등", "때", "점", "중",
        # fashion 문맥 너무 일반
        "상품", "아이템", "브랜드", "스타일", "컬렉션",
        "제품", "시즌", "디자인", "컨셉", "라인",
        # 기타
        "및", "또는", "그", "이", "저", "때문",
    }
)

_HANGUL = re.compile(r"^[\uAC00-\uD7A3]+$")


@lru_cache(maxsize=1)
def _get_mecab():
    from mecab import MeCab  # python-mecab-ko

    return MeCab()


def warmup() -> None:
    """Eager MeCab init — called from FastAPI lifespan so the first user
    query doesn't pay the dict-load tax. Idempotent via `lru_cache`.
    """
    _get_mecab().pos("warmup")


def _is_hangul(s: str) -> bool:
    return bool(_HANGUL.match(s))


def tokenize_query(text: str) -> list[str]:
    """Tokenise `text` the same way ETL tokenises `product.search_text`.

    Returns an empty list when nothing survives the filter (e.g. user only
    typed particles). Callers should fall back to the raw string in that
    case so FTS can still attempt a match on raw lexemes.
    """
    if not text:
        return []

    mecab = _get_mecab()
    tokens: list[str] = []

    for surface, tag in mecab.pos(text):
        primary = tag.split("+")[0]
        if primary not in _KEEP_POS:
            continue
        if surface in _STOP_WORDS:
            continue
        if _is_hangul(surface) and len(surface) < 2:
            continue
        tokens.append(surface)

    return tokens


def tokenize_query_text(text: str) -> str:
    """Return the joined tokens (space-separated) or the raw text when the
    tokeniser drops everything (rare — all-particle input). Keeping raw on
    empty lets `plainto_tsquery` still try against raw lexemes without
    returning an impossible empty tsquery.
    """
    tokens = tokenize_query(text)
    if not tokens:
        return text.strip()
    return " ".join(tokens)


__all__ = ["tokenize_query", "tokenize_query_text", "warmup"]
