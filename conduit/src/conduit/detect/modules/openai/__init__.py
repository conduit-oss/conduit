"""OpenAI detect module — folds former vendor-signal-registry workers."""

from __future__ import annotations

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


class OpenAIModule(DetectModule):
    name = "openai"
    packages = ["openai"]

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
                raw_list = worker.run(demo=ctx.demo, client_state=client_state)
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
