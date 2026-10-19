"""Replacement candidates for removed symbols, derived from a wheel pair.

Candidates are public callables that appear only in the newer wheel and whose
qualname shares path tokens with the removed symbol. No vendor tables.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conduit.impact.api import Symbol

# Caps keep the context pack small for the model.
MAX_CANDIDATES = 8
MAX_SIGNATURE_CHARS = 160

_TOKEN_SPLIT = re.compile(r"[._]+")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|$)|[a-z]+\d*|\d+")


def symbol_tokens(name: str) -> frozenset[str]:
    """Lowercase path/CamelCase tokens of length >= 2."""
    found: set[str] = set()
    for part in _TOKEN_SPLIT.split(name):
        if not part:
            continue
        pieces = _CAMEL.findall(part)
        if pieces:
            found.update(piece.lower() for piece in pieces if len(piece) >= 2)
        elif len(part) >= 2:
            found.add(part.lower())
    return frozenset(found)


def format_symbol_signature(sym: Symbol) -> str:
    params = list(sym.params)
    if sym.kwonly:
        params.append("*")
        params.extend(sym.kwonly)
    return f"{sym.qualname}({', '.join(params)})"


def pick_candidates(
    removed_symbol: str,
    added_signatures: dict[str, str],
    *,
    limit: int = MAX_CANDIDATES,
) -> list[dict[str, str]]:
    """Return up to ``limit`` ``{symbol, signature}`` rows sharing tokens with ``removed_symbol``.

    Tokens that appear in more than half of the added names (usually the package
    root) do not count toward the score, so a shared top-level prefix alone is
    not enough.
    """
    want = symbol_tokens(removed_symbol)
    if not want or not added_signatures:
        return []
    tokenized = {name: symbol_tokens(name) for name in added_signatures}
    df: Counter[str] = Counter()
    for toks in tokenized.values():
        df.update(toks)
    n = len(added_signatures)
    # Tokens present in most added names are too common to discriminate.
    rare = {token for token, count in df.items() if count <= max(1, n // 2)}
    useful_want = want & rare
    if not useful_want:
        useful_want = want
    scored: list[tuple[tuple[int, int, int], str, str]] = []
    for name, signature in added_signatures.items():
        shared = useful_want & tokenized[name]
        if not shared:
            continue
        score = (len(shared), sum(len(token) for token in shared), -len(name))
        scored.append((score, name, signature))
    scored.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, str]] = []
    for _score, name, signature in scored[: max(0, limit)]:
        out.append(
            {
                "symbol": name,
                "signature": signature[:MAX_SIGNATURE_CHARS],
            }
        )
    return out


def needs_replacement_candidates(*, kind: str, new: str) -> bool:
    """True when the impact has no named replacement in evidence."""
    if (new or "").strip():
        return False
    return kind in {"removed", "renamed"}
