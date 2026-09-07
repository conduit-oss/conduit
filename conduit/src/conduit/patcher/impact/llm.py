"""LLM additive impact review (JSON only)."""

from __future__ import annotations

import json
from typing import Any

from conduit.llm.client import get_llm_client
from conduit.patcher.impact.validator import validate_impact_post_rules


def llm_impact_review(
    *,
    packet: dict[str, Any],
    mechanical_findings: list[dict[str, Any]],
    planned_paths: list[str],
    file_windows: list[dict[str, Any]],
    log: Any = print,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (extra_findings, post_rules, defer_paths).

    ``defer_paths`` is always empty: LLM must not skip generic apply. Mechanical
    impact may still defer paths that have a concrete post_rule replacement.
    """
    client = get_llm_client()
    if client is None:
        return [], [], []

    windows_blob = "\n\n".join(
        f"--- {w.get('path')} ---\n{str(w.get('text', ''))[:3000]}"
        for w in file_windows[:6]
        if isinstance(w, dict)
    )
    prompt = (
        "Review migration impact before apply. Mechanical findings are authoritative; "
        "do NOT contradict or remove them. Only ADD new required impacts or post_rules.\n"
        "Do NOT emit defer_paths — generic packet apply must run first; incomplete "
        "sites are handled after apply via leftovers/repair.\n"
        f"Mechanical findings: {json.dumps(mechanical_findings[:20])}\n"
        f"Planned touch paths: {planned_paths[:40]}\n"
        f"File windows:\n{windows_blob[:10000]}\n"
        "Respond ONLY with JSON: "
        '{"impacts":[],"post_rules":[]}\n'
        "post_rules must include target_files and type (FUNCTION_BODY_REPLACE, "
        "WRAPPER_ENSURE_LIST, etc.)."
    )
    try:
        data = client.complete_json(
            system="You emit migration impact analysis as JSON only.",
            user=prompt,
        )
    except Exception as exc:
        log(f"[impact] LLM review skipped: {exc}")
        return [], [], []

    if not isinstance(data, dict):
        return [], [], []

    extra: list[dict[str, Any]] = []
    for item in data.get("impacts") or []:
        if isinstance(item, dict):
            item = dict(item, source="llm")
            extra.append(item)

    rules_raw = data.get("post_rules") or []
    rules = [dict(r, source="llm") for r in rules_raw if isinstance(r, dict)]
    errors = validate_impact_post_rules(rules, packet=packet)
    if errors:
        log(f"[impact] LLM post_rules rejected: {errors[:5]}")
        rules = []

    return extra, rules, []
