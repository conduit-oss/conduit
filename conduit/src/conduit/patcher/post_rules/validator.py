"""Validate post-rules against oracle and anticheat constraints."""

from __future__ import annotations

from typing import Any

from conduit.integrity import dummy_except_findings
from conduit.test_gen import oracle_forbidden_tokens, token_in_text


def validate_post_rule(
    rule: dict[str, Any],
    *,
    packet: dict[str, Any] | None = None,
) -> list[str]:
    """Return validation error strings; empty means OK."""
    errors: list[str] = []
    rtype = str(rule.get("type") or "")
    if not rtype:
        return ["missing type"]

    body = rule.get("body")
    if isinstance(body, str):
        for finding in dummy_except_findings(body, "body"):
            errors.append(f"body: {finding}")
        forbidden = oracle_forbidden_tokens(packet or {})
        for token in forbidden:
            if token_in_text(body, token):
                errors.append(f"body contains forbidden token {token!r}")

    targets = rule.get("target_files") or []
    if not targets:
        errors.append("missing target_files")

    return errors


def validate_post_rules(
    rules: list[dict[str, Any]],
    *,
    packet: dict[str, Any] | None = None,
) -> list[str]:
    out: list[str] = []
    for i, rule in enumerate(rules):
        for err in validate_post_rule(rule, packet=packet):
            out.append(f"post_rules[{i}]: {err}")
    return out
