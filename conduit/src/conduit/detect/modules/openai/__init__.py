"""OpenAI detect module — folds former vendor-signal-registry workers."""

from __future__ import annotations

from typing import Any

from conduit.detect.client_state import PACKAGE_PATTERN_PACKS, PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext, DetectModule
from conduit.detect.modules.openai.endpoint_compat import apply_endpoint_compat
from conduit.detect.modules.openai.evidence_seeds import (
    OPENAI_EVIDENCE_HOSTS,
    OPENAI_EVIDENCE_SEEDS,
    openai_evidence_queries,
)
from conduit.detect.modules.openai.normalize import default_rules_for, signal_to_event
from conduit.detect.modules.openai.path_param_compat import apply_path_param_compat
from conduit.detect.modules.openai.workers import ALL_WORKERS
from conduit.detect.modules.openai.workers.model_polling import ModelPollingWorker
from conduit.detect.modules.openai.workers.sdk_release import SDKReleaseWorker


def _align_dependency_bump_from_installed(
    rules: list[dict],
    *,
    installed: dict[str, str],
) -> list[dict]:
    """Prefer the consumer's declared version as DEPENDENCY_BUMP.from_version."""
    out: list[dict] = []
    for rule in rules:
        rule = dict(rule)
        if rule.get("type") == "DEPENDENCY_BUMP":
            pkg = str(rule.get("package") or "").lower()
            for name, ver in installed.items():
                if name.lower() == pkg and ver:
                    rule["from_version"] = ver
                    break
        out.append(rule)
    return out


def _is_openai_model_id(token: str) -> bool:
    text = (token or "").strip()
    if not text:
        return False
    matcher = PACKAGE_PATTERN_PACKS["openai"]["model_id"]
    hit = matcher.fullmatch(text) or matcher.search(text)
    return bool(hit and hit.group(0).lower() == text.lower())


def _model_replacements(packet: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("type") != "EXACT_STRING_REPLACE":
            continue
        match = str(rule.get("match") or "").strip()
        replace = str(rule.get("replace") or "").strip()
        if not (_is_openai_model_id(match) and _is_openai_model_id(replace)):
            continue
        key = (match, replace)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


class OpenAIModule(DetectModule):
    name = "openai"
    packages = ["openai"]

    def review_checklist(
        self,
        *,
        packet: dict[str, Any],
        report: Any,
        state: PackageClientState | None = None,
        coverage: Any = None,
    ) -> list[str]:
        items: list[str] = []
        pairs = _model_replacements(packet)
        if pairs:
            items.append(
                "Confirm replacement models are expected: "
                + ", ".join(f"{old} → {new}" for old, new in pairs)
            )
            baseline = {m.lower() for m in (state.model_ids if state else [])}
            added = [
                new
                for _, new in pairs
                if new.lower() not in baseline
            ]
            # Preserve order, drop dupes
            unique_added: list[str] = []
            for model in added:
                if model not in unique_added:
                    unique_added.append(model)
            if unique_added:
                items.append(
                    "Models added (not in client baseline): "
                    + ", ".join(unique_added)
                    + " — confirm these are the models you want"
                )
        elif state and state.model_ids:
            items.append(
                "No model string replacements in the packet; "
                "confirm client model ids were left as intended: "
                + ", ".join(state.model_ids)
            )
        return items

    def evidence_seeds(self) -> list[str]:
        return list(OPENAI_EVIDENCE_SEEDS)

    def evidence_hosts(self) -> list[str]:
        return sorted(OPENAI_EVIDENCE_HOSTS)

    def evidence_queries(self, *, from_version: str, to_version: str) -> list[str]:
        return openai_evidence_queries(from_version, to_version)

    def run(self, ctx: DetectContext) -> list[ChangeSignal]:
        warnings: list[str] = ctx.extra.setdefault("warnings", [])
        verbose_warnings: list[str] = ctx.extra.setdefault("verbose_warnings", [])
        client_state = ctx.package_states.get("openai")
        signals: list[ChangeSignal] = []
        for worker_cls in ALL_WORKERS:
            worker = worker_cls()
            try:
                raw_list = worker.run(
                    demo=ctx.demo,
                    client_state=client_state,
                    majors_only=ctx.majors_only,
                )
            except Exception as exc:
                warnings.append(f"openai worker {worker.name}: {exc}")
                continue
            if not raw_list and not ctx.demo:
                msg = f"openai worker {worker.name}: returned 0 signals"
                if isinstance(worker, ModelPollingWorker):
                    reason = worker.last_skip_reason
                    if reason == "missing_api_key":
                        warnings.append(
                            "openai worker ModelPollingWorker: no signals "
                            "(set OPENAI_API_KEY for live /v1/models polling)"
                        )
                    elif reason == "fetch_failed":
                        warnings.append(
                            "openai worker ModelPollingWorker: "
                            "GET /v1/models failed (check OPENAI_API_KEY / network)"
                        )
                    elif reason == "no_client_models":
                        verbose_warnings.append(
                            "openai worker ModelPollingWorker: no client model ids "
                            "(empty means unknown, not all-clear)"
                        )
                    else:
                        verbose_warnings.append(msg)
                elif isinstance(worker, SDKReleaseWorker):
                    reason = worker.last_skip_reason
                    if reason == "no_installed_version":
                        verbose_warnings.append(
                            "openai worker SDKReleaseWorker: no installed openai version"
                        )
                    else:
                        verbose_warnings.append(msg)
                else:
                    verbose_warnings.append(msg)
            for raw in raw_list:
                event = signal_to_event(raw)
                rules = event.rules or default_rules_for(raw)
                rules = _align_dependency_bump_from_installed(
                    list(rules), installed=ctx.installed
                )
                from_v = to_v = None
                if raw.change_type.value == "SDK_MAJOR_BUMP":
                    to_v = str(raw.extra.get("to_version") or raw.replacement_pattern or "")
                    pkg = str(raw.extra.get("package") or raw.affected_pattern or "openai")
                    from_v = None
                    for name, ver in ctx.installed.items():
                        if name.lower() == pkg.lower() and ver:
                            from_v = ver
                            break
                    from_v = from_v or str(raw.extra.get("from_version") or "")
                signals.append(
                    ChangeSignal(
                        source="module:openai",
                        package="openai",
                        change_type=event.change_type,
                        severity=event.severity,
                        from_version=from_v,
                        to_version=to_v,
                        affected_pattern=event.affected_pattern,
                        replacement_pattern=event.replacement_pattern,
                        description=event.description,
                        source_url=event.source_url,
                        deadline=event.deadline,
                        suggested_rules=list(rules),
                        hints={"event_id": event.event_id, "vendor": event.vendor},
                    )
                )

        signals, compat_notes = apply_endpoint_compat(
            signals, client_state=client_state, demo=ctx.demo
        )
        if compat_notes:
            ctx.extra.setdefault("decision_notes", []).extend(compat_notes)
            for note in compat_notes:
                verbose_warnings.append(f"openai endpoint compat: {note}")

        openapi_cache = ctx.extra.setdefault("openapi_pair_cache", {})
        signals, path_notes = apply_path_param_compat(
            signals,
            client_state=client_state,
            demo=ctx.demo,
            openapi_cache=openapi_cache,
        )
        if path_notes:
            ctx.extra.setdefault("decision_notes", []).extend(path_notes)
            for note in path_notes:
                verbose_warnings.append(f"openai path param compat: {note}")

        return signals
