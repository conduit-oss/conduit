"""Typed remint freeze obligations (shape-based, not substring tokens)."""

from __future__ import annotations

from typing import Any, Literal

AddressMode = Literal["mechanical", "manual", "missing"]


def _rules(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in (packet.get("rules") or []) if isinstance(r, dict)]


def _callee_leaf(value: str) -> str:
    text = (value or "").strip()
    return text.rsplit(".", 1)[-1] if text else ""


def has_dict_mechanical(packet: dict[str, Any]) -> bool:
    """Proof-eligible-ish CALL whose old leaf is ``dict``."""
    for rule in _rules(packet):
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        if _callee_leaf(str(rule.get("old_callee") or "")).lower() == "dict":
            return True
    return False


def has_validator_call(packet: dict[str, Any]) -> bool:
    for rule in _rules(packet):
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        if _callee_leaf(str(rule.get("old_callee") or "")) == "validator":
            return True
    return False


def has_validator_import_member(packet: dict[str, Any]) -> bool:
    for rule in _rules(packet):
        if str(rule.get("type") or "") != "AST_DECLARATION_REWRITE":
            continue
        op = rule.get("operation")
        if not isinstance(op, dict) or op.get("kind") != "import_member":
            continue
        source = op.get("source") if isinstance(op.get("source"), dict) else {}
        if str(source.get("name") or "") == "validator":
            return True
    return False


def has_validator_mechanical(packet: dict[str, Any]) -> bool:
    """Validator hop needs CALL + import_member (combined-import residual path)."""
    return has_validator_call(packet) and has_validator_import_member(packet)


def has_config_declaration(packet: dict[str, Any]) -> bool:
    for rule in _rules(packet):
        if str(rule.get("type") or "") != "AST_DECLARATION_REWRITE":
            continue
        op = rule.get("operation")
        if not isinstance(op, dict) or op.get("kind") != "inner_class_to_assignment":
            continue
        selector = op.get("selector") if isinstance(op.get("selector"), dict) else {}
        if str(selector.get("inner_name") or "") == "Config":
            return True
    return False


def has_config_manual_side_effect(packet: dict[str, Any]) -> bool:
    for effect in packet.get("side_effects") or []:
        if not isinstance(effect, dict):
            continue
        detail = str(effect.get("detail") or "").lower()
        kind = str(effect.get("kind") or "").lower()
        if "config" in kind or "config" in detail or "orm_mode" in detail:
            return True
    return False


def address_mode(
    *,
    mechanical: bool,
    manual: bool,
    manual_allowed: bool,
) -> AddressMode:
    if mechanical:
        return "mechanical"
    if manual and manual_allowed:
        return "manual"
    if manual and not manual_allowed:
        return "missing"
    return "missing"


def evaluate_gold_obligations(
    packet: dict[str, Any],
    *,
    config_manual_allowed: bool = False,
) -> dict[str, Any]:
    """
    Gold-fixture obligations (package-neutral shapes; names come from packet data).

    - dict: mechanical CALL leaf rename
    - validator: mechanical CALL + import_member
    - Config: mechanical inner_class_to_assignment (manual side_effect only if allowed)
    """
    results = {
        "dict": address_mode(
            mechanical=has_dict_mechanical(packet),
            manual=False,
            manual_allowed=False,
        ),
        "validator": address_mode(
            mechanical=has_validator_mechanical(packet),
            manual=False,
            manual_allowed=False,
        ),
        "Config": address_mode(
            mechanical=has_config_declaration(packet),
            manual=has_config_manual_side_effect(packet),
            manual_allowed=config_manual_allowed,
        ),
    }
    ok = all(mode != "missing" for mode in results.values())
    return {"ok": ok, "obligations": results}
