"""Deterministic surface metadata for minted AST_CALL_REWRITE rules."""

from __future__ import annotations

from typing import Any

_SPELLING_QUALIFIED = "qualified"
_SPELLING_IMPORTED = "imported"
_SPELLING_RECEIVER = "receiver_member"
_USE_CALL = "call"
_USE_DECORATOR = "decorator"
_ALLOWED_SPELL = frozenset(
    {_SPELLING_QUALIFIED, _SPELLING_IMPORTED, _SPELLING_RECEIVER}
)
_ALLOWED_USE = frozenset({_USE_CALL, _USE_DECORATOR})


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


def merge_surface_overlay(
    floor: dict[str, Any], overlay: Any
) -> tuple[dict[str, Any], bool]:
    """Merge optional LLM ``surface`` into the floor.

    Returns ``(surface, used_overlay)``. Invalid overlays are ignored; the floor
    is never stripped. Overlay may add spellings/use_kinds and may replace
    ``export_path`` / ``surface_id`` when those fields are valid.
    """
    if not isinstance(overlay, dict):
        return dict(floor), False

    merged = dict(floor)
    used = False

    export_path = overlay.get("export_path")
    if (
        isinstance(export_path, list)
        and export_path
        and all(isinstance(p, str) and p.strip() and _is_ident(p.strip()) for p in export_path)
    ):
        merged["export_path"] = [str(p).strip() for p in export_path]
        used = True

    spellings = _filter_enums(overlay.get("spellings"), _ALLOWED_SPELL)
    if spellings:
        merged["spellings"] = _uniq(list(merged.get("spellings") or []) + spellings)
        used = True

    use_kinds = _filter_enums(overlay.get("use_kinds"), _ALLOWED_USE)
    if use_kinds:
        merged["use_kinds"] = _uniq(list(merged.get("use_kinds") or []) + use_kinds)
        used = True

    sid = overlay.get("surface_id")
    if isinstance(sid, str) and sid.strip():
        merged["surface_id"] = sid.strip()
        used = True

    # Proof filter must still hold after merge.
    if not ({_SPELLING_IMPORTED, _SPELLING_RECEIVER} & set(merged.get("spellings") or [])):
        return dict(floor), False
    return merged, used


def enrich_minted_rules(rules: list[Any] | Any) -> list[Any]:
    """Attach floor ``surface`` to AST_CALL_REWRITE, merging any LLM overlay.

    Floor always wins as the minimum set. Overlay may only add or refine.
    Invalid overlay does not remove the floor.
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
        overlay = updated.get("surface")
        # Fully authored valid surface that already clears the proof filter:
        # still union with floor so mint never narrows below binder defaults.
        merged, _used = merge_surface_overlay(floor, overlay)
        if _valid_surface(merged):
            # Drop unknown keys; keep only schema fields (+ optional surface_id).
            clean: dict[str, Any] = {
                "export_path": list(merged["export_path"]),
                "spellings": list(merged["spellings"]),
                "use_kinds": list(merged["use_kinds"]),
            }
            if isinstance(merged.get("surface_id"), str) and merged["surface_id"]:
                clean["surface_id"] = merged["surface_id"]
            updated["surface"] = clean
        else:
            updated["surface"] = dict(floor)
        out.append(updated)
    return out


def _filter_enums(raw: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in allowed and item not in out:
            out.append(item)
    return out


def _uniq(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
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
    if not all(isinstance(s, str) and s in _ALLOWED_SPELL for s in spellings):
        return False
    if not all(isinstance(u, str) and u in _ALLOWED_USE for u in use_kinds):
        return False
    if not ({_SPELLING_IMPORTED, _SPELLING_RECEIVER} & set(spellings)):
        return False
    return True


def _is_ident(part: str) -> bool:
    if not part or part[0].isdigit():
        return False
    return all(ch.isalnum() or ch == "_" for ch in part)
