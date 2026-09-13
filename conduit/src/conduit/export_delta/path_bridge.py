"""Emit AST_CALL_REWRITE from from-tree REST paths → modern path_to_callees."""

from __future__ import annotations

from typing import Iterable

from conduit.detect.modules.openai.normalize import AST_GLOBS
from conduit.detect.modules.openai.path_callees import modern_callees_for_path
from conduit.export_delta.resources import resource_path_for
from conduit.export_delta.usage import PackageCall
from conduit.packet.rule_safety import is_valid_python_callee

PATH_BRIDGE_REASON = "Export-delta path bridge"


def rules_from_path_bridge(
    *,
    resource_paths: dict[str, str],
    calls: Iterable[PackageCall],
    removed: Iterable[str] | None = None,
    profile=None,
) -> list[dict]:
    """Rewrite when a consumer call maps to a from-tree REST path with a modern callee.

    ``removed`` is accepted for callers but is not a gate: a still-exported class
    name (Audio on 1.x) must not hide Audio.transcribe.
    """
    del removed
    rules: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for call in calls:
        path = resource_path_for(call.callee, resource_paths)
        if not path:
            continue
        modern = _pick_modern(modern_callees_for_path(path, profile=profile))
        if not modern:
            continue
        old = _bare_callee(call.callee)
        if old == modern or not is_valid_python_callee(old) or not is_valid_python_callee(modern):
            continue
        key = (old, modern)
        if key in seen:
            continue
        seen.add(key)
        rules.append(
            {
                "type": "AST_CALL_REWRITE",
                "target_files": list(AST_GLOBS),
                "old_callee": old,
                "new_callee": modern,
                "reason": f"{PATH_BRIDGE_REASON}: {old} → {path} → {modern}",
            }
        )
        prefixed = f"openai.{old}" if not old.startswith("openai.") else old
        modern_prefixed = (
            f"openai.{modern}" if not modern.startswith("openai.") else modern
        )
        if is_valid_python_callee(prefixed) and is_valid_python_callee(modern_prefixed):
            pkey = (prefixed, modern_prefixed)
            if pkey not in seen and prefixed != modern_prefixed:
                seen.add(pkey)
                rules.append(
                    {
                        "type": "AST_CALL_REWRITE",
                        "target_files": list(AST_GLOBS),
                        "old_callee": prefixed,
                        "new_callee": modern_prefixed,
                        "reason": (
                            f"{PATH_BRIDGE_REASON}: {prefixed} → {path} → {modern_prefixed}"
                        ),
                    }
                )
    return rules


def _bare_callee(callee: str) -> str:
    token = (callee or "").strip()
    if token.startswith("openai."):
        return token[7:]
    return token


def _pick_modern(candidates: list[str]) -> str | None:
    if not candidates:
        return None
    prefer = [c for c in candidates if not c.startswith("openai.")]
    return (prefer or candidates)[0]
