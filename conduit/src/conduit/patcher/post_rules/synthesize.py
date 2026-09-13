"""Orchestrate mechanical + LLM post-rule synthesis."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from conduit.patcher.post_rules.llm import synthesize_post_rules_llm
from conduit.patcher.post_rules.mechanical import infer_from_repo_scan, infer_from_test_result
from conduit.patcher.post_rules.store import (
    failure_fingerprint,
    load_learned_post_rules,
    merge_post_rules,
    save_learned_post_rules,
)
from conduit.patcher.post_rules.validator import validate_post_rules
from conduit.self_correct import build_failure_digest, collect_repair_context
from conduit.test_runner import TestResult

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def synthesize_post_rules(
    root: Path,
    packet: dict[str, Any],
    result: TestResult,
    *,
    source: dict[str, Any] | None = None,
    log: LogFn | None = None,
    use_llm: bool = True,
) -> list[dict[str, Any]]:
    """
    Infer new post-rules from a failing test run.

    Returns newly added rules (merged into .conduit/post_rules.json).
    """
    emit = log or _noop
    root = root.resolve()
    from conduit.test_runner import UNREPAIRABLE_VERIFY_KINDS, classify_verify_failure

    kind = classify_verify_failure(result, packet=packet, root=root)
    if kind in UNREPAIRABLE_VERIFY_KINDS:
        emit(f"[post-rules] skipping synthesis ({kind})")
        return []
    existing = load_learned_post_rules(root)
    repair_ctx = collect_repair_context(
        root, result, packet=packet, source=source
    )
    digest = build_failure_digest(result)
    fp = failure_fingerprint(digest.get("text") or str(result.fail_reason or ""))

    meta_path = root / ".conduit" / "post_rules_meta.json"
    if meta_path.is_file():
        import json

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if fp in (meta.get("fingerprints") or []):
                emit("[post-rules] skipping synthesis (cached fingerprint)")
                return []
        except (OSError, json.JSONDecodeError):
            pass

    mechanical = infer_from_test_result(
        result,
        file_windows=repair_ctx.file_windows,
        packet=packet,
    )
    if not mechanical and repair_ctx.seeded_paths:
        mechanical = infer_from_repo_scan(
            root, repair_ctx.seeded_paths, packet=packet
        )

    new_rules = mechanical
    if use_llm and not mechanical:
        emit("[post-rules] mechanical pass empty; trying LLM synthesis…")
        new_rules = synthesize_post_rules_llm(
            packet=packet,
            failure_digest=digest.get("text") or "",
            file_windows=repair_ctx.file_windows,
            allowlist=sorted(repair_ctx.allowlist),
            log=emit,
        )

    if not new_rules:
        return []

    fallback_targets = [
        str(w.get("path") or "").replace("\\", "/")
        for w in (repair_ctx.file_windows or [])
        if w.get("path")
    ]
    if not fallback_targets:
        fallback_targets = [str(p).replace("\\", "/") for p in repair_ctx.allowlist]
    for rule in new_rules:
        if isinstance(rule, dict) and not rule.get("target_files") and fallback_targets:
            rule["target_files"] = fallback_targets[:8]

    errors = validate_post_rules(new_rules, packet=packet)
    if errors:
        for err in errors[:5]:
            emit(f"[post-rules] rejected: {err}")
        valid: list[dict[str, Any]] = []
        for i, rule in enumerate(new_rules):
            rule_errors = [e for e in errors if e.startswith(f"post_rules[{i}]:")]
            if not rule_errors:
                valid.append(rule)
        new_rules = valid
    if not new_rules:
        return []

    merged = merge_post_rules(existing, new_rules)
    save_learned_post_rules(root, merged, failure_fingerprint=fp)
    added = [r for r in new_rules if r not in existing]
    if added:
        emit(f"[post-rules] learned {len(added)} rule(s)")
    return added
