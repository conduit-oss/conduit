"""Derive migration rules from pytest/API failures (generic, not vendor-specific)."""

from __future__ import annotations

import re
from typing import Any

from conduit.test_runner import TestResult

_TARGET_GLOBS = ["*.py", "*.ts", "*.js"]
_UNSUPPORTED_VALUE_RE = re.compile(
    r"(?i)(?:unsupported value:\s*)?'(?P<param>[\w]+)'\s+does not support\s+(?P<value>[^\s,.;]+)"
)
_UNSUPPORTED_KWARG_RE = re.compile(
    r"(?i)unexpected keyword argument\s+'(?P<param>[\w]+)'"
)
_PARAM_RENAME_HINT_RE = re.compile(
    r"(?i)'(?P<old_param>max_tokens)'\s+is not supported.*Use\s+'(?P<new_param>max_completion_tokens)'",
    re.S,
)
_NOT_SUPPORTED_PARAM_RE = re.compile(
    r"(?i)'(?P<param>[\w]+)'\s+is not supported"
)
_ONLY_DEFAULT_VALUE_RE = re.compile(
    r"(?i)'(?P<param>[\w]+)'\s+does not support.*Only the default \(1\) value is supported",
    re.S,
)
_CREATE_CALL_RE = re.compile(
    r"(?P<callee>[\w.]+)\s*\(",
)
_FLOAT_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _failure_blob(result: TestResult) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def _parse_literal(raw: str) -> Any:
    text = (raw or "").strip().strip("'\"")
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if _FLOAT_RE.match(text):
        if "." in text:
            return float(text)
        return int(text)
    return text


def _guess_function_target(
    *,
    file_windows: list[dict[str, Any]] | None,
    blob: str,
    packet: dict[str, Any] | None,
) -> str | None:
    for win in file_windows or []:
        if not isinstance(win, dict):
            continue
        callee = _guess_callee_in_text(str(win.get("text") or ""))
        if callee:
            return callee
    callee = _guess_callee_in_text(blob)
    if callee:
        return callee
    for rule in (packet or {}).get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") == "AST_CALL_REWRITE":
            new = str(rule.get("new_callee") or "").strip()
            if new:
                return new
        if str(rule.get("type") or "") == "AST_PARAM_RENAME":
            target = str(rule.get("function_target") or "").strip()
            if target:
                return target
    return None


def _guess_callee_in_text(text: str) -> str | None:
    for m in _CREATE_CALL_RE.finditer(text or ""):
        callee = m.group("callee")
        if callee.endswith(".create") or callee.endswith(".generate"):
            return callee
    return None


def _rule_key(rule: dict[str, Any]) -> tuple[Any, ...]:
    return (
        rule.get("type"),
        rule.get("function_target"),
        rule.get("param") or rule.get("old_param"),
        tuple(rule.get("values") or ()),
    )


def suggest_rules_from_failure(
    result: TestResult,
    *,
    file_windows: list[dict[str, Any]] | None = None,
    packet: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Build AST_PARAM_DROP rules from generic API error text in a test failure.

    Grounds param names and rejected values in the failure blob — no vendor literals.
    """
    blob = _failure_blob(result)
    function_target = _guess_function_target(
        file_windows=file_windows, blob=blob, packet=packet
    )
    if not function_target:
        return []

    seen: set[tuple[Any, ...]] = set()
    rules: list[dict[str, Any]] = []

    def _add(param: str, value: Any | None = None) -> None:
        param = param.strip()
        if not param:
            return
        values = [value] if value is not None else None
        rule = {
            "type": "AST_PARAM_DROP",
            "target_files": list(_TARGET_GLOBS),
            "function_target": function_target,
            "param": param,
            "reason": (
                f"Live verify failure: omit unsupported kwarg {param!r}"
                + (f"={value!r}" if value is not None else "")
                + "."
            ),
        }
        if values is not None:
            rule["values"] = values
        key = _rule_key(rule)
        if key in seen:
            return
        seen.add(key)
        rules.append(rule)

    for pattern in (_UNSUPPORTED_VALUE_RE, _NOT_SUPPORTED_PARAM_RE, _UNSUPPORTED_KWARG_RE):
        for m in pattern.finditer(blob):
            param = m.group("param")
            blob_low = blob.lower()
            param_low = param.lower()
            if param_low == "max_tokens" and "temperature" in blob_low and "max_tokens" not in blob_low:
                continue
            if param_low == "max_tokens" and "max_completion_tokens" in blob_low and "max_tokens" not in blob_low:
                continue
            if pattern is _UNSUPPORTED_VALUE_RE:
                _add(param, _parse_literal(m.group("value")))
            else:
                _add(param)

    for m in _ONLY_DEFAULT_VALUE_RE.finditer(blob):
        _add(m.group("param"))

    existing = {
        _rule_key(r)
        for r in (packet or {}).get("rules") or []
        if isinstance(r, dict) and r.get("type") == "AST_PARAM_DROP"
    }
    drop_rules = [r for r in rules if _rule_key(r) not in existing]

    rename_rules: list[dict[str, Any]] = []
    for m in _PARAM_RENAME_HINT_RE.finditer(blob):
        old_p = m.group("old_param")
        new_p = m.group("new_param")
        if not function_target or not old_p or not new_p:
            continue
        rename = {
            "type": "AST_PARAM_RENAME",
            "target_files": list(_TARGET_GLOBS),
            "function_target": function_target,
            "old_param": old_p,
            "new_param": new_p,
            "reason": f"Live verify failure: rename kwarg {old_p!r} -> {new_p!r}.",
        }
        rename_key = _rule_key(rename)
        already = any(
            isinstance(r, dict)
            and r.get("type") == "AST_PARAM_RENAME"
            and r.get("function_target") == function_target
            and r.get("old_param") == old_p
            for r in (packet or {}).get("rules") or []
        )
        if not already and rename_key not in seen:
            rename_rules.append(rename)

    return drop_rules + rename_rules
