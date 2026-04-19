"""tsquery string builder with synonym expansion.

`plainto_tsquery` doesn't support OR between synonyms. To expand `자켓` →
`(자켓 | 재킷 | jacket)` we must use `to_tsquery`, which parses a formal
query string. This module assembles that string safely — sanitising token
characters and escaping before handing to Postgres.
"""

from __future__ import annotations

import re

from app.services._synonyms import expand_token

# tsquery lexemes can only contain word chars / Hangul. Strip everything else
# so a stray `&` or `!` from user input never becomes a tsquery operator.
_SAFE_LEXEME = re.compile(r"[\W_]+", re.UNICODE)


def _sanitize(token: str) -> str:
    """Keep letters / digits / Hangul. Drops everything else including spaces."""
    return _SAFE_LEXEME.sub("", token)


def build_tsquery_string(tokens: list[str]) -> str:
    """Join tokens as `(t1 | syn1 | …) & (t2 | syn2 | …)`.

    Empty tokens or tokens that strip to empty are skipped. When the result is
    empty (e.g. every token sanitised away), returns an empty string — callers
    should bail back to FTS-skip.
    """
    groups: list[str] = []
    for token in tokens:
        clean = _sanitize(token)
        if not clean:
            continue
        variants = [_sanitize(v) for v in expand_token(clean)]
        variants = [v for v in variants if v]
        if not variants:
            continue
        if len(variants) == 1:
            groups.append(variants[0])
        else:
            groups.append("(" + " | ".join(dict.fromkeys(variants)) + ")")
    return " & ".join(groups)


__all__ = ["build_tsquery_string"]
