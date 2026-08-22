"""Usage-scoped SDK callee migrations derived from path maps and endpoint signals."""

from __future__ import annotations

import re
from typing import Any, Iterable

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.normalize import AST_GLOBS
from conduit.detect.modules.openai.path_callees import (
    callees_for_path,
    normalize_api_path,
    path_for_api_pattern,
)
from conduit.detect.modules.openai.workers.base import resolve_profile
from conduit.packet.rule_safety import is_valid_python_callee

_LEGACY_CALLEE_RE = re.compile(
    r"(?:^|[.])(?:ChatCompletion|Completion|Edit|Engine|FineTune|Image|Moderation)"
    r"(?:[.]create|[.]list|[.]retrieve)?$",
    re.I,
)


def _collect_used_callees(
    *,
    api_patterns: Iterable[str] | None,
    extra: Iterable[str] | None = None,
) -> set[str]:
    out: set[str] = set()
    for raw in list(api_patterns or []) + list(extra or []):
        token = str(raw or "").strip()
        if not token or token.startswith("/"):
            continue
        if token.lower() == "chat.completions":
            out.add("chat.completions.create")
        elif token == "ChatCompletion":
            out.add("ChatCompletion.create")
        else:
            out.add(token)
    return out


def _callee_in_scope(old_callee: str, used: set[str]) -> bool:
    if not used:
        return True
    low = old_callee.lower()
    for item in used:
        if item.lower() == low:
            return True
        if item.lower().endswith("." + low.split(".")[-1]):
            return True
    return False


def _call_rewrite_rule(
    *,
    old_callee: str,
    new_callee: str,
    reason: str,
) -> dict[str, Any] | None:
    if not is_valid_python_callee(old_callee) or not is_valid_python_callee(new_callee):
        return None
    return {
        "type": "AST_CALL_REWRITE",
        "target_files": list(AST_GLOBS),
        "old_callee": old_callee,
        "new_callee": new_callee,
        "reason": reason,
    }


def _same_path_legacy_pairs(
    api_patterns: Iterable[str] | None,
    *,
    profile=None,
) -> list[tuple[str, str, str]]:
    """Legacy callees on a path → modern first target for that path."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    used = _collect_used_callees(api_patterns=api_patterns)
    prof = resolve_profile(profile)
    for token in sorted(used):
        path = path_for_api_pattern(token, profile=prof)
        if not path:
            continue
        targets = callees_for_path(path, api_patterns=api_patterns, profile=prof)
        if not targets:
            continue
        modern = targets[0]
        candidates = {token}
        if not token.endswith(".create") and "." not in token:
            candidates.add(f"{token}.create")
        for old in candidates:
            if old == modern:
                continue
            if not _LEGACY_CALLEE_RE.search(old) and old not in {
                "Completion.create",
                "ChatCompletion.create",
            }:
                continue
            key = (old, modern)
            if key in seen:
                continue
            seen.add(key)
            reason = (
                f"SDK callee {old} → {modern} for {path} "
                "(derived from path/callee map)."
            )
            out.append((old, modern, reason))
    return out


def apply_sdk_callee_migration(
    signals: list[ChangeSignal],
    *,
    client_state: PackageClientState | None = None,
    profile=None,
) -> tuple[list[ChangeSignal], list[str]]:
    """
    Emit AST_CALL_REWRITE signals from endpoint path pairs + client api_patterns.

    When no client usage is known (publisher catalog), rules come from endpoint
    A→B pairs only. When api_patterns are present, rewrites are usage-scoped.
    """
    notes: list[str] = []
    api_patterns = list(client_state.api_patterns) if client_state else []
    used = _collect_used_callees(api_patterns=api_patterns)
    prof = resolve_profile(profile)
    pkg = (client_state.package if client_state else None) or (
        resolve_profile(profile).packages[0]
        if resolve_profile(profile).packages
        else "openai"
    )

    rewrites: dict[tuple[str, str], str] = {}

    for signal in signals:
        if signal.change_type != "API_BREAKING":
            continue
        old_path = normalize_api_path(signal.affected_pattern)
        new_path = normalize_api_path(signal.replacement_pattern)
        if not old_path or not new_path or old_path == new_path:
            continue
        new_targets = callees_for_path(new_path, api_patterns=api_patterns, profile=prof)
        if not new_targets:
            continue
        new_callee = new_targets[0]
        old_targets = callees_for_path(old_path, api_patterns=api_patterns, profile=prof)
        if not old_targets:
            old_targets = [
                c
                for c in callees_for_path(old_path, api_patterns=None, profile=prof)
                if _LEGACY_CALLEE_RE.search(c) or c.endswith(".create")
            ]
        source_url = signal.source_url or ""
        for old_callee in old_targets:
            if old_callee == new_callee:
                continue
            if used and not _callee_in_scope(old_callee, used):
                continue
            reason = (
                f"Endpoint {old_path} → {new_path}; migrate callee "
                f"{old_callee} → {new_callee}."
            )
            if source_url:
                reason += f" Source: {source_url}"
            rewrites[(old_callee, new_callee)] = reason

    has_sdk_bump = any(
        s.change_type in {"SDK_MAJOR_BUMP", "SDK_BUMP"} for s in signals
    )
    if has_sdk_bump:
        for old, new, reason in _same_path_legacy_pairs(
            api_patterns, profile=prof
        ):
            if used and not _callee_in_scope(old, used):
                continue
            rewrites.setdefault((old, new), reason)

    if not rewrites:
        return signals, notes

    rules = [
        rule
        for (old, new), reason in sorted(rewrites.items())
        if (rule := _call_rewrite_rule(old_callee=old, new_callee=new, reason=reason))
    ]
    notes.append(
        f"SDK callee migration: {len(rules)} AST_CALL_REWRITE rule(s)"
        + (f" (scoped to {len(used)} usage pattern(s))" if used else "")
    )
    out = list(signals)
    out.append(
        ChangeSignal(
            source="module:openai:sdk_callee_migration",
            package=str(pkg),
            change_type="SDK_CALLEE_MIGRATION",
            severity="CRITICAL",
            description=notes[-1],
            suggested_rules=rules,
        )
    )
    return out, notes
