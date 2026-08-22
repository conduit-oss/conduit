"""Doc-driven packet augmentation at conduit run (no hardcoded rule literals)."""

from __future__ import annotations

import re
from typing import Any, Iterable

from conduit.detect.modules.openai.path_callees import (
    callees_for_path,
    modern_callees_for_path,
    normalize_api_path,
)
from conduit.detect.modules.openai.sdk_callee_migration import (
    _call_rewrite_rule,
    _collect_used_callees,
    _callee_in_scope,
    _LEGACY_CALLEE_RE,
    _same_path_legacy_pairs,
    pick_modern_callee,
)
from conduit.detect.modules.openai.workers.base import resolve_profile
from conduit.packet.migration_evidence import build_migration_evidence
from conduit.packet.rule_safety import is_valid_python_callee
from conduit.packet.synthesize import merge_packet_rules

_PATH_PAIR_RE = re.compile(
    r"(/v1/[\w./_-]+)\s*(?:→|->|(?:\s+)(?:to|replaced by|use)\s+)\s*(/v1/[\w./_-]+)",
    re.I,
)
_PATH_TOKEN_RE = re.compile(r"/v1/[\w./_-]+")


def parse_path_pairs_from_docs(text: str, *, source_url: str = "") -> list[tuple[str, str, str]]:
    """Extract old→new API path pairs from deprecations / migration markdown."""
    pairs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for old, new in _PATH_PAIR_RE.findall(text or ""):
        old_p = normalize_api_path(old)
        new_p = normalize_api_path(new)
        if not old_p or not new_p or old_p == new_p:
            continue
        key = (old_p, new_p)
        if key in seen:
            continue
        seen.add(key)
        reason = f"Endpoint {old_p} → {new_p} per migration docs."
        if source_url:
            reason += f" Source: {source_url}"
        pairs.append((old_p, new_p, reason))
    return pairs


def derive_callee_rules(
    *,
    path_pairs: Iterable[tuple[str, str, str]],
    api_patterns: Iterable[str] | None = None,
    profile=None,
) -> list[dict[str, Any]]:
    """Build AST_CALL_REWRITE rules from path successor pairs + client usage."""
    prof = resolve_profile(profile)
    used = _collect_used_callees(api_patterns=api_patterns)
    # old_callee -> (new_callee, reason); path pairs win over same-path pairs
    rewrites: dict[str, tuple[str, str]] = {}

    for old_path, new_path, reason in path_pairs:
        modern_targets = modern_callees_for_path(new_path, profile=prof)
        if not modern_targets:
            continue
        old_targets = callees_for_path(old_path, api_patterns=api_patterns, profile=prof)
        if not old_targets:
            old_targets = [
                c
                for c in callees_for_path(old_path, api_patterns=None, profile=prof)
                if _LEGACY_CALLEE_RE.search(c) or c.endswith(".create")
            ]
        for old_callee in old_targets:
            new_callee = pick_modern_callee(old_callee, modern_targets)
            if not new_callee or old_callee == new_callee:
                continue
            if used and not _callee_in_scope(old_callee, used):
                continue
            if not is_valid_python_callee(old_callee) or not is_valid_python_callee(new_callee):
                continue
            rewrites.setdefault(old_callee, (new_callee, reason))

    has_usage = bool(used)
    if has_usage:
        for old, new, reason in _same_path_legacy_pairs(api_patterns, profile=prof):
            if not _callee_in_scope(old, used):
                continue
            if not is_valid_python_callee(old) or not is_valid_python_callee(new):
                continue
            rewrites.setdefault(old, (new, reason))

    return [
        rule
        for old, (new, reason) in sorted(rewrites.items())
        if (rule := _call_rewrite_rule(old_callee=old, new_callee=new, reason=reason))
    ]


def _no_rule_context(
    no_rule_items: Iterable[dict[str, Any]] | None,
    source: dict[str, Any] | None,
) -> list[str]:
    chunks: list[str] = []
    for item in no_rule_items or []:
        if not isinstance(item, dict):
            continue
        chunks.append(f"{item.get('kind')} {item.get('value')} {item.get('detail')}")
    src = source or {}
    chunks.extend(str(x) for x in src.get("model_ids") or [])
    for usage in src.get("usages") or []:
        if isinstance(usage, dict):
            chunks.append(str(usage.get("pattern") or usage.get("value") or ""))
    chunks.extend(str(x) for x in src.get("api_patterns") or [])
    return chunks


def augment_packet_from_docs(
    packet: dict[str, Any],
    source: dict[str, Any] | None,
    *,
    no_rule_items: Iterable[dict[str, Any]] | None = None,
    log=None,
) -> tuple[dict[str, Any], list[str]]:
    """
    Merge doc-derived rules into a client-bound packet before scope pruning.

    Returns (packet, log_lines).
    """
    pkg = str(packet.get("package") or "")
    if not pkg:
        return packet, []

    try:
        from conduit.detect.vendor_profile import profile_for_package
    except Exception:
        return packet, []

    profile = profile_for_package(pkg)
    if profile is None:
        return packet, []

    context = _no_rule_context(no_rule_items, source)
    if not context:
        return packet, []

    evidence = build_migration_evidence(
        context_chunks=context,
        profile=profile,
        max_pages=6,
        demo_openapi=True,
    )

    path_pairs: list[tuple[str, str, str]] = []
    for doc in evidence.docs:
        path_pairs.extend(
            parse_path_pairs_from_docs(doc.text, source_url=doc.url)
        )
    # Also infer paths mentioned near "deprecated" in doc text
    for doc in evidence.docs:
        if "deprecat" not in (doc.text or "").lower():
            continue
        paths = _PATH_TOKEN_RE.findall(doc.text or "")
        for idx, path in enumerate(paths):
            if idx + 1 < len(paths):
                old_p = normalize_api_path(path)
                new_p = normalize_api_path(paths[idx + 1])
                if old_p and new_p and old_p != new_p:
                    reason = f"Endpoint {old_p} → {new_p} per migration docs. Source: {doc.url}"
                    path_pairs.append((old_p, new_p, reason))

    api_patterns = list((source or {}).get("api_patterns") or [])
    rules = derive_callee_rules(
        path_pairs=path_pairs,
        api_patterns=api_patterns,
        profile=profile,
    )
    if not rules:
        return packet, evidence.warnings

    out = dict(packet)
    out["rules"] = merge_packet_rules(list(out.get("rules") or []), rules)
    lines = [
        f"Doc-augmented {len(rules)} rule(s) from developers.openai.com "
        f"({len(list(no_rule_items or []))} gap(s))"
    ]
    if log:
        for line in lines:
            log(f"[conduit] {line}")
    return out, lines + evidence.warnings
