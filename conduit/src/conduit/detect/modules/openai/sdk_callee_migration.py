"""Usage-scoped SDK callee migrations derived from path maps and endpoint signals."""

from __future__ import annotations

import re
from typing import Any, Iterable

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.normalize import AST_GLOBS
from conduit.detect.modules.openai.path_callees import (
    callees_for_path,
    modern_callees_for_path,
    normalize_api_path,
    path_for_api_pattern,
)
from conduit.detect.modules.openai.workers.base import resolve_profile
from conduit.packet.rule_safety import is_valid_python_callee

_LEGACY_CALLEE_RE = re.compile(
    r"(?:^|[.])(?:ChatCompletion|Completion|Edit|Engine|FineTune|Image|Moderation|"
    r"Embedding|File)"
    r"(?:[.]create|[.]list|[.]retrieve)?$",
    re.I,
)

_CALLEE_VERBS = frozenset(
    {"create", "list", "retrieve", "generate", "edit", "create_edit"}
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


def _callee_verb(name: str) -> str | None:
    seg = str(name or "").strip().rsplit(".", 1)
    if not seg:
        return None
    verb = seg[-1].lower()
    return verb if verb in _CALLEE_VERBS else None


def pick_modern_callee(old_callee: str, candidates: list[str]) -> str | None:
    """Pick a structural successor: same verb + openai. prefix preferred."""
    if not candidates:
        return None
    verb = _callee_verb(old_callee)
    want_openai = str(old_callee or "").startswith("openai.")

    def _rank(cand: str) -> tuple[int, int, int]:
        legacy = 1 if _LEGACY_CALLEE_RE.search(cand) else 0
        verb_miss = 0
        if verb:
            verb_miss = 0 if _callee_verb(cand) == verb else 1
        else:
            verb_miss = 0
        if want_openai:
            prefix_miss = 0 if cand.startswith("openai.") else 1
        else:
            prefix_miss = 0 if not cand.startswith("openai.") else 1
        return (legacy, verb_miss, prefix_miss)

    return min(candidates, key=_rank)


def _same_path_legacy_pairs(
    api_patterns: Iterable[str] | None,
    *,
    profile=None,
) -> list[tuple[str, str, str]]:
    """Legacy callees on a path → modern map target for that path."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    used = _collect_used_callees(api_patterns=api_patterns)
    prof = resolve_profile(profile)
    for token in sorted(used):
        path = path_for_api_pattern(token, profile=prof)
        if not path:
            continue
        modern_targets = modern_callees_for_path(path, profile=prof)
        if not modern_targets:
            continue
        candidates = {token}
        if not token.endswith(".create") and "." not in token:
            candidates.add(f"{token}.create")
        for old in candidates:
            if not _LEGACY_CALLEE_RE.search(old) and old not in {
                "Completion.create",
                "ChatCompletion.create",
            }:
                continue
            modern = pick_modern_callee(old, modern_targets)
            if not modern or old == modern:
                continue
            if _LEGACY_CALLEE_RE.search(modern):
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

    rewrites: dict[str, tuple[str, str]] = {}

    for signal in signals:
        if signal.change_type != "API_BREAKING":
            continue
        old_path = normalize_api_path(signal.affected_pattern)
        new_path = normalize_api_path(signal.replacement_pattern)
        if not old_path or not new_path or old_path == new_path:
            continue
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
        source_url = signal.source_url or ""
        for old_callee in old_targets:
            if not _LEGACY_CALLEE_RE.search(old_callee):
                continue
            new_callee = pick_modern_callee(old_callee, modern_targets)
            if not new_callee or old_callee == new_callee:
                continue
            if _LEGACY_CALLEE_RE.search(new_callee):
                continue
            if used and not _callee_in_scope(old_callee, used):
                continue
            reason = (
                f"Endpoint {old_path} → {new_path}; migrate callee "
                f"{old_callee} → {new_callee}."
            )
            if source_url:
                reason += f" Source: {source_url}"
            rewrites.setdefault(old_callee, (new_callee, reason))

    has_sdk_bump = any(
        s.change_type in {"SDK_MAJOR_BUMP", "SDK_BUMP"} for s in signals
    )
    if has_sdk_bump:
        for old, new, reason in _same_path_legacy_pairs(
            api_patterns, profile=prof
        ):
            if used and not _callee_in_scope(old, used):
                continue
            rewrites.setdefault(old, (new, reason))

    if not rewrites:
        return signals, notes

    rules = [
        rule
        for old, (new, reason) in sorted(rewrites.items())
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
