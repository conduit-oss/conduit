"""Deterministic surface metadata for minted AST_CALL_REWRITE rules."""

from __future__ import annotations

from typing import Any

_SPELLING_QUALIFIED = "qualified"
_SPELLING_IMPORTED = "imported"
_SPELLING_RECEIVER = "receiver_member"
_USE_CALL = "call"
_USE_DECORATOR = "decorator"


def derive_surface_floor(old_callee: str, new_callee: str = "") -> dict[str, Any] | None:
    """Build binder-aligned surface defaults from lexical callees.

    Dotted exports include ``receiver_member`` so typed ``self.dict()`` stays
    searchable. Bare names include ``imported`` and decorator use so
    ``@validator`` is proof-eligible. Spellings always intersect the binder's
    proof set ``{imported, receiver_member}``.
    """
    old = (old_callee or "").strip()
    if not old:
        return None
    export_path = [p for p in old.split(".") if p]
    if not export_path:
        return None
    if not all(_is_ident(p) for p in export_path):
        return None

    spellings = [_SPELLING_QUALIFIED, _SPELLING_IMPORTED]
    if len(export_path) >= 2:
        spellings.append(_SPELLING_RECEIVER)
    use_kinds = [_USE_CALL, _USE_DECORATOR]
    _ = new_callee  # rewrite target stays on the rule; binder recomputes Rewrite
    return {
        "export_path": export_path,
        "spellings": spellings,
        "use_kinds": use_kinds,
    }


def enrich_minted_rules(rules: list[Any] | Any) -> list[Any]:
    """Attach floor ``surface`` to AST_CALL_REWRITE rules that lack a valid one.

    Authored / already-valid ``surface`` objects are left alone. Does not strip
    a bad surface back to lexical-only when a floor can be derived: invalid
    surface is replaced by the floor.
    """
    if not isinstance(rules, list):
        return []
    out: list[Any] = []
    for rule in rules:
        if not isinstance(rule, dict):
            out.append(rule)
            continue
        if str(rule.get("type") or "").strip() != "AST_CALL_REWRITE":
            out.append(rule)
            continue
        updated = dict(rule)
        floor = derive_surface_floor(
            str(updated.get("old_callee") or ""),
            str(updated.get("new_callee") or ""),
        )
        if floor is None:
            out.append(updated)
            continue
        existing = updated.get("surface")
        if _valid_surface(existing):
            out.append(updated)
            continue
        updated["surface"] = floor
        out.append(updated)
    return out


def _valid_surface(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    export_path = value.get("export_path")
    spellings = value.get("spellings")
    use_kinds = value.get("use_kinds")
    if not isinstance(export_path, list) or not export_path:
        return False
    if not all(isinstance(p, str) and p.strip() for p in export_path):
        return False
    if not isinstance(spellings, list) or not spellings:
        return False
    if not isinstance(use_kinds, list) or not use_kinds:
        return False
    allowed_spell = {_SPELLING_QUALIFIED, _SPELLING_IMPORTED, _SPELLING_RECEIVER}
    allowed_use = {_USE_CALL, _USE_DECORATOR}
    if not all(isinstance(s, str) and s in allowed_spell for s in spellings):
        return False
    if not all(isinstance(u, str) and u in allowed_use for u in use_kinds):
        return False
    # Must clear binder proof filter.
    if not ({_SPELLING_IMPORTED, _SPELLING_RECEIVER} & set(spellings)):
        return False
    return True


def _is_ident(part: str) -> bool:
    if not part or part[0].isdigit():
        return False
    return all(ch.isalnum() or ch == "_" for ch in part)
