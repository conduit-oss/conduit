"""LLM fallback post-rule synthesis (JSON rules only, no direct writes)."""

from __future__ import annotations

import json
from typing import Any

from conduit.llm.client import get_llm_client
from conduit.patcher.post_rules.validator import validate_post_rules
from conduit.test_gen import oracle_forbidden_tokens


def synthesize_post_rules_llm(
    *,
    packet: dict[str, Any],
    failure_digest: str,
    file_windows: list[dict[str, Any]],
    allowlist: list[str],
    log: Any = print,
) -> list[dict[str, Any]]:
    """Ask LLM for post-rules JSON; return validated rules only."""
    client = get_llm_client()
    if client is None:
        return []

    forbidden = oracle_forbidden_tokens(packet)
    windows_blob = "\n\n".join(
        f"--- {w.get('path')} ---\n{w.get('text', '')[:4000]}"
        for w in file_windows[:8]
        if isinstance(w, dict)
    )
    prompt = (
        "Propose post_rules JSON array to fix migration test failures.\n"
        "Allowed types: WRAPPER_ENSURE_LIST, WRAPPER_ENSURE_DICT_KEY, "
        "WRAPPER_DELEGATE, FUNCTION_BODY_REPLACE, RUNTIME_MODEL_ALIAS.\n"
        "Do NOT include forbidden legacy tokens in any body text.\n"
        "Do NOT use except Exception that returns dummy values.\n"
        f"Forbidden tokens sample: {forbidden[:20]}\n"
        f"runtime_model_aliases: {json.dumps(packet.get('runtime_model_aliases') or {})}\n"
        f"Allowlist: {allowlist[:40]}\n"
        f"Failure digest:\n{failure_digest[:6000]}\n"
        f"File windows:\n{windows_blob[:12000]}\n"
        "Respond with ONLY a JSON object: {\"post_rules\": [ ... ]}"
    )
    try:
        data = client.complete_json(
            system="You emit migration post_rules as JSON only.",
            user=prompt,
        )
    except Exception as exc:
        log(f"[post-rules] LLM synthesis skipped: {exc}")
        return []

    rules = data.get("post_rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        log("[post-rules] LLM returned no post_rules array")
        return []

    cleaned = [dict(r, source="llm") for r in rules if isinstance(r, dict)]
    errors = validate_post_rules(cleaned, packet=packet)
    if errors:
        log(f"[post-rules] LLM rules rejected: {errors[:5]}")
        return []
    return cleaned
