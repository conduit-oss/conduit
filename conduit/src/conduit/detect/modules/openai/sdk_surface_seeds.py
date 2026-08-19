"""Known OpenAI SDK call-surface rules for catalog snapshots.

Documented 0.x / openai@3 migrations only — no invented model successors.
"""

from __future__ import annotations

from typing import Any

from conduit.detect.modules.openai.normalize import AST_GLOBS

_REASON_PY = (
    "Documented openai Python SDK 0.x → modern client: "
    "https://github.com/openai/openai-python/discussions/742"
)
_REASON_JS = (
    "Documented openai Node SDK v3 → v4: "
    "https://github.com/openai/openai-node/discussions/217"
)


def catalog_sdk_surface_rules(*, package: str, ecosystem: str) -> list[dict[str, Any]]:
    if str(package or "").lower() != "openai":
        return []
    eco = str(ecosystem or "").strip().lower()
    if eco == "pypi":
        return [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": list(AST_GLOBS),
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
                "reason": _REASON_PY,
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": list(AST_GLOBS),
                "old_callee": "Completion.create",
                "new_callee": "chat.completions.create",
                "reason": _REASON_PY,
            },
            {
                "type": "AST_PARAM_RENAME",
                "target_files": list(AST_GLOBS),
                "function_target": "chat.completions.create",
                "old_param": "max_tokens",
                "new_param": "max_completion_tokens",
                "reason": _REASON_PY,
            },
            {
                "type": "AST_PARAM_RENAME",
                "target_files": list(AST_GLOBS),
                "function_target": "chat.completions.create",
                "old_param": "functions",
                "new_param": "tools",
                "reason": _REASON_PY,
            },
            {
                "type": "AST_PARAM_RENAME",
                "target_files": list(AST_GLOBS),
                "function_target": "chat.completions.create",
                "old_param": "function_call",
                "new_param": "tool_choice",
                "reason": _REASON_PY,
            },
        ]
    if eco == "npm":
        return [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": list(AST_GLOBS),
                "old_callee": "createChatCompletion",
                "new_callee": "chat.completions.create",
                "reason": _REASON_JS,
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": list(AST_GLOBS),
                "old_callee": "createCompletion",
                "new_callee": "chat.completions.create",
                "reason": _REASON_JS,
            },
            {
                "type": "AST_IMPORT_REWRITE",
                "target_files": list(AST_GLOBS),
                "old_import": "OpenAIApi",
                "new_import": "OpenAI",
                "reason": _REASON_JS,
            },
            {
                "type": "AST_IMPORT_REWRITE",
                "target_files": list(AST_GLOBS),
                "old_import": "Configuration",
                "new_import": "OpenAI",
                "reason": _REASON_JS,
            },
            {
                "type": "AST_PARAM_RENAME",
                "target_files": list(AST_GLOBS),
                "function_target": "chat.completions.create",
                "old_param": "max_tokens",
                "new_param": "max_completion_tokens",
                "reason": _REASON_JS,
            },
        ]
    return []


def _seed_key(rule: dict[str, Any]) -> tuple[str, ...]:
    rtype = str(rule.get("type") or "")
    if rtype == "AST_CALL_REWRITE":
        return (rtype, str(rule.get("old_callee") or "").lower())
    if rtype == "AST_PARAM_RENAME":
        return (
            rtype,
            str(rule.get("function_target") or "").lower(),
            str(rule.get("old_param") or "").lower(),
        )
    if rtype == "AST_IMPORT_REWRITE":
        return (rtype, str(rule.get("old_import") or "").lower())
    if rtype == "EXACT_STRING_REPLACE":
        return (rtype, str(rule.get("match") or "").lower())
    return (rtype, str(rule))


def merge_catalog_sdk_surface_rules(
    rules: list[dict[str, Any]],
    *,
    package: str,
    ecosystem: str,
) -> list[dict[str, Any]]:
    seeds = catalog_sdk_surface_rules(package=package, ecosystem=ecosystem)
    if not seeds:
        return list(rules)
    seen = {_seed_key(r) for r in rules if isinstance(r, dict)}
    out = list(rules)
    for seed in seeds:
        key = _seed_key(seed)
        if key in seen:
            continue
        seen.add(key)
        out.append(seed)
    return out
