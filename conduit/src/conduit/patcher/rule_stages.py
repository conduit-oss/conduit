"""Partition packet rules into apply stages (SDK before REST)."""

from __future__ import annotations

from typing import Any, Literal

ApplyStage = Literal["sdk", "rest"]

SDK_RULE_TYPES = frozenset(
    {
        "DEPENDENCY_BUMP",
        "DEPENDENCY_ADD",
        "DEPENDENCY_REMOVE",
        "AST_CALL_REWRITE",
        "AST_ATTR_RENAME",
        "AST_PARAM_RENAME",
        "AST_PARAM_DROP",
        "AST_IMPORT_REWRITE",
    }
)

REST_RULE_TYPES = frozenset(
    {
        "EXACT_STRING_REPLACE",
        "REGEX_REPLACE",
        "KEY_RENAME",
    }
)


def rule_stage(rule: dict[str, Any]) -> ApplyStage | None:
    rtype = str(rule.get("type") or "")
    if rtype in SDK_RULE_TYPES:
        return "sdk"
    if rtype in REST_RULE_TYPES:
        return "rest"
    return None


def partition_rules(
    rules: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """
    Split rules into (sdk_rules, rest_rules, unknown_warnings).

    Preserves relative order within each stage.
    """
    sdk: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    warnings: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        stage = rule_stage(rule)
        if stage == "sdk":
            sdk.append(rule)
        elif stage == "rest":
            rest.append(rule)
        else:
            rtype = str(rule.get("type") or "UNKNOWN")
            msg = f"unknown rule type skipped during apply: {rtype}"
            if msg not in warnings:
                warnings.append(msg)
    return sdk, rest, warnings
