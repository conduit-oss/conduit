"""Scope catalog packet rules to client source-packet usage."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from conduit.packet.synthesize import _DEP_RULE_TYPES, _source_usage_index

_OLD_TOKEN_KEYS = {
    "EXACT_STRING_REPLACE": "match",
    "AST_PARAM_RENAME": "old_param",
    "AST_PARAM_DROP": "param",
    "AST_IMPORT_REWRITE": "old_import",
    "AST_ATTR_RENAME": "old_attr",
    "AST_CALL_REWRITE": "old_callee",
    "KEY_RENAME": "old_key",
}


@dataclass
class RuleScopeResult:
    rules: list[dict[str, Any]]
    kept: int
    total: int
    collapsed: int = 0


def collapse_replace_chains(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Rewrite ``A→B`` + ``B→C`` into ``A→C`` (and ``B→C``) so hops land on the end."""
    hops: dict[str, str] = {}
    hops_l: dict[str, str] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "EXACT_STRING_REPLACE":
            continue
        match = str(rule.get("match") or "").strip()
        replace = str(rule.get("replace") or "").strip()
        if match and replace and match.lower() != replace.lower():
            hops[match] = replace
            hops_l[match.lower()] = replace

    def _final(start: str) -> str:
        seen: set[str] = set()
        cur = start
        while cur.lower() in hops_l:
            key = cur.lower()
            if key in seen:
                break
            seen.add(key)
            nxt = hops_l[key]
            if nxt.lower() == key:
                break
            cur = nxt
        return cur

    finals: dict[str, str] = {}
    for match in hops:
        finals[match] = _final(match)

    collapsed = 0
    out: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        item = dict(rule)
        if str(item.get("type") or "") == "EXACT_STRING_REPLACE":
            match = str(item.get("match") or "").strip()
            old_rep = str(item.get("replace") or "").strip()
            new_rep = finals.get(match, old_rep)
            if new_rep and new_rep != old_rep:
                item["replace"] = new_rep
                collapsed += 1
        out.append(item)
    return out, collapsed


def _rule_old_tokens(rule: dict[str, Any]) -> list[str]:
    rtype = str(rule.get("type") or "")
    key = _OLD_TOKEN_KEYS.get(rtype)
    if not key:
        return []
    val = str(rule.get(key) or "").strip()
    return [val] if val else []


def _token_in_index(token: str, index: dict[str, Any]) -> bool:
    low = token.lower()
    if low in index["models"] or low in index["callees"]:
        return True
    from conduit.detect.modules.openai.path_callees import (
        normalize_api_path,
        path_for_api_pattern,
    )

    mapped = path_for_api_pattern(token) or (
        normalize_api_path(token if token.startswith("/") else f"/{token}")
    )
    if mapped and mapped.lower() in index["paths"]:
        return True
    if low in index["paths"]:
        return True
    return False


def filter_rules_to_source(
    rules: list[dict[str, Any]],
    source: dict[str, Any] | None,
) -> RuleScopeResult:
    """Keep dependency rules plus usage-matching string/AST/key rules.

    ``REGEX_REPLACE`` is kept (unsafe to prune). Empty source usage keeps all.
    """
    collapsed, n_collapsed = collapse_replace_chains(list(rules or []))
    total = len(collapsed)
    index = _source_usage_index(source)
    if not index["has_usage"]:
        return RuleScopeResult(
            rules=collapsed, kept=total, total=total, collapsed=n_collapsed
        )

    out: list[dict[str, Any]] = []
    for rule in collapsed:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "")
        if rtype in _DEP_RULE_TYPES or rtype == "REGEX_REPLACE":
            out.append(rule)
            continue
        tokens = _rule_old_tokens(rule)
        if not tokens:
            out.append(rule)
            continue
        if any(_token_in_index(t, index) for t in tokens):
            out.append(rule)
    return RuleScopeResult(
        rules=out, kept=len(out), total=total, collapsed=n_collapsed
    )


def scope_packet_to_source(
    packet: dict[str, Any],
    source: dict[str, Any] | None,
) -> tuple[dict[str, Any], RuleScopeResult]:
    scoped = copy.deepcopy(packet)
    result = filter_rules_to_source(list(scoped.get("rules") or []), source)
    scoped["rules"] = result.rules
    return scoped, result
