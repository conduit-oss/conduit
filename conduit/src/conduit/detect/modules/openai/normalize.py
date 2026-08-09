"""Normalize raw worker signals into unified registry events with rules.

Only emit apply rules when both sides are known (no invented path/call targets).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from conduit.detect.modules.openai.models_legacy import (
    ChangeType,
    RawSignal,
    RegistryDocument,
    RegistryEvent,
    Severity,
)

DEFAULT_CODE_GLOBS = ["*.py", "*.ts", "*.js", "*.yaml", "*.yml", "*.json", ".env*"]
AST_GLOBS = ["*.py", "*.ts", "*.js"]

_PATH_RE = re.compile(r"^/v1/[A-Za-z0-9/_\-{}]+$")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return cleaned[:48] or "item"


def make_event_id(signal: RawSignal) -> str:
    base = (
        f"evt_{_slug(signal.vendor)}_{_slug(signal.change_type.value)}_"
        f"{_slug(signal.affected_pattern)}"
    )
    digest = hashlib.sha1(
        f"{signal.vendor}|{signal.change_type.value}|{signal.affected_pattern}|"
        f"{signal.replacement_pattern}|{signal.deadline}".encode()
    ).hexdigest()[:6]
    return f"{base}_{digest}"


def _looks_like_api_path(value: str | None) -> bool:
    if not value:
        return False
    return bool(_PATH_RE.match(value.strip()))


def _param_function_targets(signal: RawSignal) -> list[str]:
    """Return explicit targets only — never invent ChatCompletion vs modern paths."""
    extra = signal.extra or {}
    targets = extra.get("function_targets")
    if isinstance(targets, list) and targets:
        return [str(t) for t in targets if t]
    single = extra.get("function_target")
    if single:
        return [str(single)]
    return []


def _rule_reason(signal: RawSignal, *, fallback: str) -> str:
    extra = signal.extra or {}
    for key in ("reason", "endpoint_compat_reason"):
        raw = extra.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    if signal.description and signal.description.strip():
        return signal.description.strip()
    if signal.source_url:
        return f"{fallback} Source: {signal.source_url}"
    return fallback


def _with_reason(rule: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(rule)
    if reason and not out.get("reason"):
        out["reason"] = reason
    return out


def default_rules_for(signal: RawSignal) -> list[dict[str, Any]]:
    if signal.suggested_rules:
        reason = _rule_reason(signal, fallback="From detect signal")
        return [_with_reason(dict(r), reason) for r in signal.suggested_rules]

    rules: list[dict[str, Any]] = []

    # Model ID / literal migrations only — never string-replace package names
    if signal.change_type in {
        ChangeType.MODEL_DEPRECATION,
        ChangeType.MODEL_REMOVED,
    }:
        if signal.replacement_pattern and signal.affected_pattern:
            reason = _rule_reason(
                signal,
                fallback=(
                    f"Replace deprecated/removed model {signal.affected_pattern} "
                    f"with {signal.replacement_pattern}."
                ),
            )
            rules.append(
                _with_reason(
                    {
                        "type": "EXACT_STRING_REPLACE",
                        "target_files": list(DEFAULT_CODE_GLOBS),
                        "match": signal.affected_pattern,
                        "replace": signal.replacement_pattern,
                    },
                    reason,
                )
            )

    if signal.change_type == ChangeType.PARAM_RENAME:
        old_param = signal.extra.get("old_param") or signal.affected_pattern
        new_param = signal.extra.get("new_param") or signal.replacement_pattern
        targets = _param_function_targets(signal)
        if old_param and new_param and targets:
            reason = _rule_reason(
                signal,
                fallback=f"Rename parameter {old_param} → {new_param}.",
            )
            for function_target in targets:
                rules.append(
                    _with_reason(
                        {
                            "type": "AST_PARAM_RENAME",
                            "target_files": list(AST_GLOBS),
                            "function_target": function_target,
                            "old_param": old_param,
                            "new_param": new_param,
                        },
                        reason,
                    )
                )

    if signal.change_type == ChangeType.SDK_MAJOR_BUMP:
        package = signal.extra.get("package", signal.affected_pattern)
        from_version = signal.extra.get("from_version", "0.0.0")
        to_version = signal.extra.get("to_version") or signal.replacement_pattern or "1.0.0"
        reason = _rule_reason(
            signal,
            fallback=f"Bump {package} dependency {from_version} → {to_version}.",
        )
        rules.append(
            _with_reason(
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": package,
                    "from_version": from_version,
                    "to_version": to_version,
                    "ecosystems": signal.extra.get("ecosystems", ["pip", "npm", "pyproject"]),
                },
                reason,
            )
        )

    if signal.change_type == ChangeType.API_BREAKING:
        old_path = signal.affected_pattern
        new_path = signal.replacement_pattern
        # Both sides required — do not invent successors.
        if _looks_like_api_path(old_path) and _looks_like_api_path(new_path):
            reason = _rule_reason(
                signal,
                fallback=f"Replace API path {old_path.strip()} → {new_path.strip()}.",
            )
            rules.append(
                _with_reason(
                    {
                        "type": "EXACT_STRING_REPLACE",
                        "target_files": list(DEFAULT_CODE_GLOBS),
                        "match": old_path.strip(),
                        "replace": new_path.strip(),
                    },
                    reason,
                )
            )

    return rules


def signal_to_event(signal: RawSignal) -> RegistryEvent:
    return RegistryEvent(
        event_id=make_event_id(signal),
        vendor=signal.vendor,
        change_type=signal.change_type.value,
        severity=signal.severity.value
        if isinstance(signal.severity, Severity)
        else str(signal.severity),
        affected_pattern=signal.affected_pattern,
        replacement_pattern=signal.replacement_pattern,
        deadline=signal.deadline,
        source_url=signal.source_url,
        description=signal.description,
        rules=default_rules_for(signal),
    )


def merge_signals(
    signals: list[RawSignal],
    *,
    generated_at: str,
    version: str = "1.0.0",
) -> RegistryDocument:
    events: list[RegistryEvent] = []
    for signal in signals:
        events.append(signal_to_event(signal))
    return RegistryDocument(
        version=version,
        generated_at=generated_at,
        events=events,
    )
