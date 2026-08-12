"""Validate / adjust model replacements against Supported endpoints docs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.model_docs import (
    MODELS_CATALOG_URL,
    fetch_model_endpoints,
    fetch_models_catalog,
    map_api_patterns_to_routes,
    model_doc_url,
    model_supports_routes,
    prefer_catalog_candidates,
    unsupported_routes,
)
from conduit.detect.modules.openai.workers.base import fixtures_dir

_MAX_CANDIDATE_FETCHES = 5


def _demo_text(relative: str) -> str | None:
    path = fixtures_dir() / "model_docs" / relative
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def _demo_api_patterns(client_state: PackageClientState | None) -> list[str]:
    if client_state and client_state.api_patterns:
        return list(client_state.api_patterns)
    used_path = fixtures_dir() / "models" / "client_used.json"
    if used_path.is_file():
        data = json.loads(used_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return [str(x) for x in data.get("api_patterns") or []]
    return []


def _load_endpoints(
    model_id: str,
    *,
    demo: bool,
    client: httpx.Client | None,
    cache: dict[str, dict[str, bool] | None],
) -> dict[str, bool] | None:
    key = model_id.lower()
    if key in cache:
        return cache[key]
    if demo:
        text = _demo_text(f"{model_id}.md")
        # Missing fixture → unknown (None), not empty-unsupported.
        endpoints = (
            fetch_model_endpoints(model_id, text=text) if text is not None else None
        )
    else:
        endpoints = fetch_model_endpoints(model_id, client=client)
    cache[key] = endpoints
    return endpoints


def _load_catalog(*, demo: bool, client: httpx.Client | None) -> list[str]:
    if demo:
        text = _demo_text("models.md") or ""
        return fetch_models_catalog(text=text)
    return fetch_models_catalog(client=client)


def _reason_keep(
    legacy: str,
    replacement: str,
    required: list[str],
    *,
    source_url: str | None,
) -> str:
    routes = ", ".join(required) if required else "(no client api patterns; baseline unknown)"
    src = source_url or model_doc_url(replacement)
    return (
        f"Deprecated model {legacy}; keep documented replacement {replacement} "
        f"because it supports required endpoint(s) [{routes}]. Source: {src}"
    )


def _reason_alternate(
    legacy: str,
    rejected: str,
    chosen: str,
    missing: list[str],
    required: list[str],
) -> str:
    return (
        f"Deprecated model {legacy}; deprecation suggested {rejected}, but "
        f"{rejected} does not support [{', '.join(missing)}]. "
        f"Chose {chosen} which supports required endpoint(s) "
        f"[{', '.join(required)}]. "
        f"Docs: {model_doc_url(rejected)} ; {model_doc_url(chosen)} ; {MODELS_CATALOG_URL}"
    )


def _reason_cleared(legacy: str, rejected: str | None, missing: list[str], required: list[str]) -> str:
    rej = rejected or "(none)"
    caps = [c for c in (missing or required) if c]
    if not caps:
        return (
            f"Deprecated/removed model {legacy}; no replacement emitted. "
            f"Suggested {rej}; endpoint docs/requirements unavailable to validate. "
            f"Checked catalog {MODELS_CATALOG_URL} for alternates."
        )
    return (
        f"Deprecated/removed model {legacy}; no replacement emitted. "
        f"Suggested {rej} missing support for [{', '.join(caps)}]. "
        f"Checked catalog {MODELS_CATALOG_URL} for alternates supporting "
        f"[{', '.join(required)}]."
    )


def _pick_alternate(
    *,
    rejected: str,
    required: list[str],
    demo: bool,
    client: httpx.Client | None,
    cache: dict[str, dict[str, bool] | None],
) -> str | None:
    catalog = _load_catalog(demo=demo, client=client)
    candidates = prefer_catalog_candidates(
        catalog,
        required_routes=required,
        rejected=rejected,
        limit=_MAX_CANDIDATE_FETCHES,
    )
    for mid in candidates:
        endpoints = _load_endpoints(mid, demo=demo, client=client, cache=cache)
        # Skip unknown docs — cannot verify support.
        if model_supports_routes(endpoints, required) is True:
            return mid
    return None


def _required_routes_for_model(
    client_state: PackageClientState | None,
    model_id: str,
    fallback: list[str],
) -> list[str]:
    """Routes this model is actually called with; not the union of all client APIs."""
    if client_state is None:
        return list(fallback)
    tokens: list[str] = []
    mid = (model_id or "").lower()
    for raw in client_state.usages or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("id") or "").lower() != mid:
            continue
        tokens.extend(str(x) for x in (raw.get("paths") or []) if x)
        tokens.extend(str(x) for x in (raw.get("callees") or []) if x)
    per_site = map_api_patterns_to_routes(tokens)
    return per_site or list(fallback)


def apply_endpoint_compat(
    signals: list[ChangeSignal],
    *,
    client_state: PackageClientState | None = None,
    demo: bool = False,
) -> tuple[list[ChangeSignal], list[str]]:
    """
    Adjust MODEL_* replacements using per-model Supported endpoints.

    Returns (signals, notes) where notes are decision-log lines for the packet/PR.
    """
    notes: list[str] = []
    api_patterns = (
        list(client_state.api_patterns)
        if client_state and client_state.api_patterns
        else (_demo_api_patterns(client_state) if demo else [])
    )
    fallback_required = map_api_patterns_to_routes(api_patterns)

    model_signals = [
        s
        for s in signals
        if s.change_type in {"MODEL_DEPRECATION", "MODEL_REMOVED"}
        and s.affected_pattern
    ]
    if not model_signals:
        return signals, notes

    cache: dict[str, dict[str, bool] | None] = {}
    out: list[ChangeSignal] = []
    touched_ids = {id(s) for s in model_signals}

    with httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "conduit-endpoint-compat/0.1"},
        follow_redirects=True,
    ) as client:
        http: httpx.Client | None = None if demo else client
        for signal in signals:
            if id(signal) not in touched_ids:
                out.append(signal)
                continue

            legacy = str(signal.affected_pattern)
            replacement = signal.replacement_pattern
            source_url = signal.source_url
            required = _required_routes_for_model(
                client_state, legacy, fallback_required
            )

            # No usable client endpoints → keep scrape replacement; still attach reason.
            if not required:
                reason = (
                    f"Deprecated/removed model {legacy}; "
                    f"using documented replacement {replacement or '(none)'} "
                    f"(client api patterns unknown — skipped endpoint check)."
                )
                notes.append(reason)
                out.append(_with_reason(signal, replacement, reason, source_url))
                continue

            if not replacement:
                alt = _pick_alternate(
                    rejected=legacy,
                    required=required,
                    demo=demo,
                    client=http,
                    cache=cache,
                )
                if alt:
                    reason = (
                        f"Model {legacy} missing/removed with no deprecation replacement; "
                        f"chose {alt} supporting [{', '.join(required)}]. "
                        f"Source: {model_doc_url(alt)}"
                    )
                    notes.append(reason)
                    out.append(_with_reason(signal, alt, reason, model_doc_url(alt)))
                else:
                    reason = _reason_cleared(legacy, None, [], required)
                    notes.append(reason)
                    out.append(_with_reason(signal, None, reason, source_url))
                continue

            endpoints = _load_endpoints(
                replacement, demo=demo, client=http, cache=cache
            )
            support = model_supports_routes(endpoints, required)
            # Unknown docs → keep documented replacement (fail-open).
            if support is None:
                reason = (
                    f"Deprecated/removed model {legacy}; "
                    f"using documented replacement {replacement} "
                    f"(endpoint docs unavailable — skipped endpoint check). "
                    f"Source: {source_url or model_doc_url(replacement)}"
                )
                notes.append(reason)
                out.append(
                    _with_reason(
                        signal,
                        replacement,
                        reason,
                        source_url or model_doc_url(replacement),
                    )
                )
                continue
            if support is True:
                reason = _reason_keep(
                    legacy, replacement, required, source_url=source_url
                )
                notes.append(reason)
                out.append(
                    _with_reason(
                        signal,
                        replacement,
                        reason,
                        source_url or model_doc_url(replacement),
                    )
                )
                continue

            missing = unsupported_routes(endpoints, required) or list(required)
            alt = _pick_alternate(
                rejected=replacement,
                required=required,
                demo=demo,
                client=http,
                cache=cache,
            )
            if alt:
                reason = _reason_alternate(
                    legacy, replacement, alt, missing, required
                )
                notes.append(reason)
                out.append(_with_reason(signal, alt, reason, model_doc_url(alt)))
            else:
                reason = _reason_cleared(legacy, replacement, missing, required)
                notes.append(reason)
                out.append(_with_reason(signal, None, reason, source_url))

    return out, notes


def _with_reason(
    signal: ChangeSignal,
    replacement: str | None,
    reason: str,
    source_url: str | None,
) -> ChangeSignal:
    rules: list[dict[str, Any]] = []
    for rule in signal.suggested_rules:
        rule = dict(rule)
        if (
            rule.get("type") == "EXACT_STRING_REPLACE"
            and rule.get("match") == signal.affected_pattern
        ):
            if replacement:
                rule["replace"] = replacement
                rule["reason"] = reason
                rules.append(rule)
            # drop model replace when cleared
            continue
        if not rule.get("reason"):
            rule["reason"] = reason
        rules.append(rule)

    if replacement and signal.affected_pattern and not any(
        r.get("type") == "EXACT_STRING_REPLACE"
        and r.get("match") == signal.affected_pattern
        for r in rules
    ):
        rules.append(
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": [
                    "*.py",
                    "*.ts",
                    "*.js",
                    "*.yaml",
                    "*.yml",
                    "*.json",
                    ".env*",
                ],
                "match": signal.affected_pattern,
                "replace": replacement,
                "reason": reason,
            }
        )

    return ChangeSignal(
        source=signal.source,
        package=signal.package,
        change_type=signal.change_type,
        severity=signal.severity,
        from_version=signal.from_version,
        to_version=signal.to_version,
        ecosystem=signal.ecosystem,
        affected_pattern=signal.affected_pattern,
        replacement_pattern=replacement,
        description=reason,
        source_url=source_url or signal.source_url,
        deadline=signal.deadline,
        hints={**signal.hints, "endpoint_compat_reason": reason},
        suggested_rules=rules,
    )


def decision_notes_path() -> Path:
    """Unused helper kept for tests discovering fixture root."""
    return fixtures_dir() / "model_docs"
