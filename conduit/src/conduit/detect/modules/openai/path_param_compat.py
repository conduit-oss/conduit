"""When endpoints change, check OpenAPI request params on old vs new paths."""

from __future__ import annotations

from typing import Any

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.normalize import AST_GLOBS
from conduit.detect.modules.openai.path_callees import (
    callees_for_path,
    looks_like_api_path,
    normalize_api_path,
)
from conduit.detect.modules.openai.workers.openapi_diff import (
    OPENAPI_SOURCE_URL,
    diff_path_params,
    load_openapi_pair,
)


def _path_pairs(signals: list[ChangeSignal]) -> list[tuple[str, str, ChangeSignal | None]]:
    """
    Return (old_path, new_path, source_signal) pairs.

    Prefer API_BREAKING with both sides. Join OpenAPI removals (no replacement)
    with a deprecation/other signal that supplies the successor for the same path.
    """
    pairs: list[tuple[str, str, ChangeSignal | None]] = []
    seen: set[tuple[str, str]] = set()

    replacements: dict[str, tuple[str, ChangeSignal]] = {}
    removals: list[ChangeSignal] = []

    for s in signals:
        if s.change_type != "API_BREAKING":
            continue
        old = normalize_api_path(s.affected_pattern)
        if not old:
            continue
        new = normalize_api_path(s.replacement_pattern)
        if new:
            replacements[old] = (new, s)
            key = (old, new)
            if key not in seen:
                seen.add(key)
                pairs.append((old, new, s))
        else:
            removals.append(s)

    for s in removals:
        old = normalize_api_path(s.affected_pattern)
        if not old:
            continue
        if old in replacements:
            new, src = replacements[old]
            key = (old, new)
            if key not in seen:
                seen.add(key)
                pairs.append((old, new, src))
            # Annotate removal signal description with successor note later via notes
            continue
        # No successor — leave as-is (no param invent)

    return pairs


def _existing_param_keys(signals: list[ChangeSignal]) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for s in signals:
        if s.change_type != "PARAM_RENAME":
            continue
        old = str(s.affected_pattern or "")
        new = str(s.replacement_pattern or "")
        path = ""
        if isinstance(s.hints, dict):
            path = str(s.hints.get("path") or "")
        keys.add((old, new, path))
        for rule in s.suggested_rules:
            if rule.get("type") == "AST_PARAM_RENAME":
                keys.add(
                    (
                        str(rule.get("old_param") or old),
                        str(rule.get("new_param") or new),
                        str(rule.get("function_target") or path),
                    )
                )
    return keys


def _param_signal(
    *,
    old_path: str,
    new_path: str,
    old_param: str,
    new_param: str,
    api_patterns: list[str],
    source_url: str | None,
) -> ChangeSignal:
    targets = callees_for_path(new_path, api_patterns=api_patterns)
    reason = (
        f"Endpoint {old_path} → {new_path}; request property {old_param} → {new_param} "
        f"per OpenAPI schemas. Source: {source_url or OPENAPI_SOURCE_URL}"
    )
    rules: list[dict[str, Any]] = []
    for target in targets:
        rules.append(
            {
                "type": "AST_PARAM_RENAME",
                "target_files": list(AST_GLOBS),
                "function_target": target,
                "old_param": old_param,
                "new_param": new_param,
                "reason": reason,
            }
        )
    return ChangeSignal(
        source="module:openai",
        package="openai",
        change_type="PARAM_RENAME",
        severity="CRITICAL",
        affected_pattern=old_param,
        replacement_pattern=new_param,
        description=reason,
        source_url=source_url or OPENAPI_SOURCE_URL,
        hints={
            "vendor": "openai",
            "path": new_path,
            "old_path": old_path,
            "new_path": new_path,
            "endpoint_param_compat": True,
        },
        suggested_rules=rules,
    )


def apply_path_param_compat(
    signals: list[ChangeSignal],
    *,
    client_state: PackageClientState | None = None,
    demo: bool = False,
    openapi_cache: dict | None = None,
) -> tuple[list[ChangeSignal], list[str]]:
    """
    For endpoint A→B pairs, diff OpenAPI request props and emit param renames.

    Returns (signals, decision_notes).
    """
    notes: list[str] = []
    pairs = _path_pairs(signals)

    # Note pure removals (no successor) — never invent params for them.
    paired_olds = {old for old, _new, _src in pairs}
    for s in signals:
        if (
            s.change_type == "API_BREAKING"
            and looks_like_api_path(s.affected_pattern)
            and not s.replacement_pattern
        ):
            old = normalize_api_path(s.affected_pattern)
            if old and old not in paired_olds:
                notes.append(
                    f"Endpoint {s.affected_pattern} removed with no documented successor; "
                    f"skipped param compat (no invent)."
                )

    if not pairs:
        return signals, notes

    pair_specs = load_openapi_pair(demo=demo, cache=openapi_cache)
    if not pair_specs:
        notes.append("path param compat skipped (OpenAPI pair unavailable)")
        return signals, notes

    previous, latest = pair_specs
    api_patterns = list(client_state.api_patterns) if client_state else []
    existing = _existing_param_keys(signals)
    out = list(signals)

    for old_path, new_path, src in pairs:
        diff = diff_path_params(previous, latest, old_path, new_path)
        source_url = (src.source_url if src else None) or OPENAPI_SOURCE_URL

        if diff.renames:
            for old_p, new_p in diff.renames:
                # Deduplicate against shared-path renames already present
                if (old_p, new_p, new_path) in existing or (old_p, new_p, old_path) in existing:
                    notes.append(
                        f"Param {old_p} → {new_p} already signaled for "
                        f"{old_path} → {new_path}; kept existing."
                    )
                    continue
                # Also skip if same rename exists on any path
                if any(k[0] == old_p and k[1] == new_p for k in existing):
                    # Enrich missing function_targets on existing PARAM_RENAME if needed
                    enriched = False
                    for i, s in enumerate(out):
                        if (
                            s.change_type == "PARAM_RENAME"
                            and s.affected_pattern == old_p
                            and s.replacement_pattern == new_p
                        ):
                            if s.suggested_rules and any(
                                r.get("function_target") for r in s.suggested_rules
                            ):
                                continue
                            # Rebuild rules with targets
                            neo = _param_signal(
                                old_path=old_path,
                                new_path=new_path,
                                old_param=old_p,
                                new_param=new_p,
                                api_patterns=api_patterns,
                                source_url=source_url,
                            )
                            if neo.suggested_rules:
                                out[i] = ChangeSignal(
                                    source=s.source,
                                    package=s.package,
                                    change_type=s.change_type,
                                    severity=s.severity,
                                    from_version=s.from_version,
                                    to_version=s.to_version,
                                    ecosystem=s.ecosystem,
                                    affected_pattern=s.affected_pattern,
                                    replacement_pattern=s.replacement_pattern,
                                    description=neo.description,
                                    source_url=s.source_url or neo.source_url,
                                    deadline=s.deadline,
                                    hints={**s.hints, **neo.hints},
                                    suggested_rules=neo.suggested_rules,
                                )
                                enriched = True
                                notes.append(
                                    f"Attached function_target(s) for {old_p} → {new_p} "
                                    f"({old_path} → {new_path})."
                                )
                    if enriched:
                        existing.add((old_p, new_p, new_path))
                    continue

                sig = _param_signal(
                    old_path=old_path,
                    new_path=new_path,
                    old_param=old_p,
                    new_param=new_p,
                    api_patterns=api_patterns,
                    source_url=source_url,
                )
                out.append(sig)
                existing.add((old_p, new_p, new_path))
                notes.append(sig.description or "")
        else:
            if diff.removed:
                note = (
                    f"Endpoint {old_path} → {new_path}: removed request props "
                    f"{', '.join(diff.removed)} with no 1:1 rename "
                    f"(added: {', '.join(diff.added) or 'none'}). No invent."
                )
                notes.append(note)
                # Annotate the path signal description when present
                for i, s in enumerate(out):
                    if (
                        s.change_type == "API_BREAKING"
                        and normalize_api_path(s.affected_pattern) == old_path
                        and normalize_api_path(s.replacement_pattern) == new_path
                    ):
                        desc = (s.description or "").rstrip()
                        out[i] = ChangeSignal(
                            source=s.source,
                            package=s.package,
                            change_type=s.change_type,
                            severity=s.severity,
                            from_version=s.from_version,
                            to_version=s.to_version,
                            ecosystem=s.ecosystem,
                            affected_pattern=s.affected_pattern,
                            replacement_pattern=s.replacement_pattern,
                            description=f"{desc}\n{note}".strip() if desc else note,
                            source_url=s.source_url,
                            deadline=s.deadline,
                            hints=s.hints,
                            suggested_rules=list(s.suggested_rules),
                        )
                        break
            else:
                notes.append(
                    f"Endpoint {old_path} → {new_path}: request props compatible "
                    f"(no param renames needed)."
                )

    return out, [n for n in notes if n]
