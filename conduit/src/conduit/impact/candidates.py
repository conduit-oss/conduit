"""Replacement candidates for removed symbols, derived from a wheel pair.

Candidates are public callables that appear only in the newer wheel. Ranking
uses shared name tokens, leaf-name match, real parameters vs empty class
signatures, and overlap with the removed symbol's parameters / call-site
kwargs. No vendor tables.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conduit.impact.api import Symbol

# Caps keep the context pack small for the model.
MAX_CANDIDATES = 8
MAX_SIGNATURE_CHARS = 160

_TOKEN_SPLIT = re.compile(r"[._]+")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|$)|[a-z]+\d*|\d+")
_SKIP_PARAMS = frozenset({"self", "cls", "args", "kwargs"})


def _fold_plural(token: str) -> set[str]:
    """Simple singular/plural forms so completion ↔ completions match."""
    forms = {token}
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        forms.add(token[:-1])
    else:
        forms.add(token + "s")
    return forms


def symbol_tokens(name: str) -> frozenset[str]:
    """Lowercase path/CamelCase tokens of length >= 2, with plural folding."""
    found: set[str] = set()
    for part in _TOKEN_SPLIT.split(name):
        if not part:
            continue
        pieces = _CAMEL.findall(part)
        if not pieces:
            pieces = [part]
        for piece in pieces:
            if len(piece) < 2:
                continue
            found.update(_fold_plural(piece.lower()))
    return frozenset(found)


def leaf_name(symbol: str) -> str:
    return symbol.rsplit(".", 1)[-1]


def format_symbol_signature(sym: Symbol) -> str:
    params = list(sym.params)
    if sym.kwonly:
        params.append("*")
        params.extend(sym.kwonly)
    return f"{sym.qualname}({', '.join(params)})"


def symbol_param_names(sym: Symbol) -> tuple[str, ...]:
    return tuple(name for name in (*sym.params, *sym.kwonly) if name not in _SKIP_PARAMS)


def pick_candidates(
    removed_symbol: str,
    added_signatures: dict[str, str],
    *,
    added_params: dict[str, tuple[str, ...]] | None = None,
    old_params: Iterable[str] | None = None,
    call_kwargs: Iterable[str] | None = None,
    limit: int = MAX_CANDIDATES,
) -> list[dict[str, str]]:
    """Return up to ``limit`` ``{symbol, signature}`` replacement rows.

    Prefer callables with real parameters, leaf-name matches, shared path
    tokens (package-root tokens that dominate the added set are down-weighted),
    and parameter overlap with the removed signature / call-site kwargs.
    """
    want = symbol_tokens(removed_symbol)
    params_map = added_params or {}
    wanted_params = {
        name
        for name in (*(old_params or ()), *(call_kwargs or ()))
        if name and name not in _SKIP_PARAMS
    }
    if not added_signatures:
        return []
    if not want and not wanted_params:
        return []

    tokenized = {name: symbol_tokens(name) for name in added_signatures}
    df: Counter[str] = Counter()
    for toks in tokenized.values():
        df.update(toks)
    n = len(added_signatures)
    rare = {token for token, count in df.items() if count <= max(1, n // 2)}
    useful_want = (want & rare) or want

    removed_leaf = _fold_plural(leaf_name(removed_symbol).lower())
    scored: list[tuple[tuple[int, ...], str, str]] = []
    for name, signature in added_signatures.items():
        shared = useful_want & tokenized[name] if useful_want else frozenset()
        cand_params = {p for p in params_map.get(name, ()) if p not in _SKIP_PARAMS}
        # Fall back to parsing the signature string when params map is absent.
        if not cand_params:
            cand_params = _params_from_signature(signature)
        param_overlap = len(wanted_params & cand_params) if wanted_params else 0
        if not shared and param_overlap == 0:
            continue
        leaf = leaf_name(name).lower()
        leaf_match = 1 if leaf in removed_leaf or bool(_fold_plural(leaf) & removed_leaf) else 0
        n_params = len(cand_params)
        has_params = 1 if n_params > 0 else 0
        # Demote empty class/TypedDict-style signatures.
        empty_sig = 1 if n_params == 0 else 0
        score = (
            param_overlap,
            leaf_match,
            has_params,
            len(shared),
            sum(len(token) for token in shared),
            -empty_sig,
            -len(name),
        )
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


def _params_from_signature(signature: str) -> set[str]:
    if "(" not in signature or not signature.endswith(")"):
        return set()
    inner = signature[signature.index("(") + 1 : -1].strip()
    if not inner:
        return set()
    names: set[str] = set()
    for part in inner.split(","):
        part = part.strip()
        if not part or part == "*":
            continue
        name = part.lstrip("*").split("=", 1)[0].strip()
        if name and name not in _SKIP_PARAMS:
            names.add(name)
    return names


def needs_replacement_candidates(*, kind: str, new: str) -> bool:
    """True when the impact has no named replacement in evidence."""
    if (new or "").strip():
        return False
    return kind in {"removed", "renamed"}
