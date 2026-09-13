"""Validate impact LLM proposals (reuse post-rule validator)."""

from __future__ import annotations

from typing import Any

from conduit.patcher.post_rules.validator import validate_post_rules


def validate_impact_post_rules(
    rules: list[dict[str, Any]],
    *,
    packet: dict[str, Any] | None = None,
) -> list[str]:
    return validate_post_rules(rules, packet=packet)
