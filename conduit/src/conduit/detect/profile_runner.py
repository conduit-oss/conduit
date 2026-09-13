"""Shared detect-module runner driven by a VendorProfile."""

from __future__ import annotations

from typing import Any

from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext
from conduit.detect.modules.openai.endpoint_compat import apply_endpoint_compat
from conduit.detect.modules.openai.normalize import default_rules_for, signal_to_event
from conduit.detect.modules.openai.path_param_compat import apply_path_param_compat
from conduit.detect.modules.openai.sdk_callee_migration import apply_sdk_callee_migration
from conduit.detect.modules.openai.workers.model_polling import ModelPollingWorker
from conduit.detect.modules.openai.workers.sdk_release import (
    SDKReleaseWorker,
    packet_ecosystem_for,
)
from conduit.detect.vendor_profile import VendorProfile


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


def stock_workers_for(profile: VendorProfile) -> list[type]:
    """Enable generic (and OpenAI-HTML) workers based on which profile URLs are set."""
    from conduit.detect.modules.openai.workers.changelog_parser import (
        ChangelogParserWorker,
    )
    from conduit.detect.modules.openai.workers.deprecation_scraper import (
        DeprecationScraperWorker,
    )
    from conduit.detect.modules.openai.workers.openapi_diff import OpenAPIDiffWorker

    workers: list[type] = []
    if profile.openapi_repo or profile.openapi_source_url:
        workers.append(OpenAPIDiffWorker)
    if profile.deprecations_url and profile.parser_kind == "openai_html":
        workers.append(DeprecationScraperWorker)
    if profile.live_catalog_url:
        workers.append(ModelPollingWorker)
    if profile.changelog_url and profile.parser_kind == "openai_html":
        workers.append(ChangelogParserWorker)
    if profile.sdk_release_repos:
        workers.append(SDKReleaseWorker)
    return workers


def _client_state_for(ctx: DetectContext, profile: VendorProfile) -> Any:
    states = ctx.package_states or {}
    for pkg in profile.packages:
        state = states.get(pkg) or states.get(pkg.lower())
        if state is not None:
            return state
    for key, state in states.items():
        if profile.matches_package(key):
            return state
    return None


def run_profile_module(
    ctx: DetectContext,
    *,
    profile: VendorProfile,
    workers: list[type] | None = None,
) -> list[ChangeSignal]:
    """Run workers → normalize → optional endpoint/path compat for ``profile``."""
    warnings: list[str] = ctx.extra.setdefault("warnings", [])
    verbose_warnings: list[str] = ctx.extra.setdefault("verbose_warnings", [])
    client_state = _client_state_for(ctx, profile)
    pkg = (profile.packages[0] if profile.packages else profile.name)
    label = profile.name
    worker_list = workers if workers is not None else stock_workers_for(profile)
    signals: list[ChangeSignal] = []

    for worker_cls in worker_list:
        worker = worker_cls()
        try:
            raw_list = worker.run(
                demo=ctx.demo,
                client_state=client_state,
                majors_only=ctx.majors_only,
                profile=profile,
                catalog_latest=ctx.catalog_latest,
            )
        except TypeError:
            try:
                raw_list = worker.run(
                    demo=ctx.demo,
                    client_state=client_state,
                    majors_only=ctx.majors_only,
                    profile=profile,
                )
            except TypeError:
                raw_list = worker.run(
                    demo=ctx.demo,
                    client_state=client_state,
                    majors_only=ctx.majors_only,
                )
        except Exception as exc:
            warnings.append(f"{label} worker {worker.name}: {exc}")
            continue
        if not raw_list and not ctx.demo:
            msg = f"{label} worker {worker.name}: returned 0 signals"
            if isinstance(worker, ModelPollingWorker):
                reason = worker.last_skip_reason
                auth = profile.live_catalog_auth_env or "API_KEY"
                catalog = profile.live_catalog_url or "live catalog"
                if reason == "missing_api_key":
                    warnings.append(
                        f"{label} worker ModelPollingWorker: no signals "
                        f"(set {auth} for live {catalog} polling)"
                    )
                elif reason == "fetch_failed":
                    warnings.append(
                        f"{label} worker ModelPollingWorker: "
                        f"{catalog} failed (check {auth} / network)"
                    )
                elif reason == "no_client_models":
                    verbose_warnings.append(
                        f"{label} worker ModelPollingWorker: no client model ids "
                        "(empty means unknown, not all-clear)"
                    )
                else:
                    verbose_warnings.append(msg)
            elif isinstance(worker, SDKReleaseWorker):
                reason = worker.last_skip_reason
                if reason == "no_installed_version":
                    verbose_warnings.append(
                        f"{label} worker SDKReleaseWorker: no installed {pkg} version"
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
            eco = None
            if raw.change_type.value == "SDK_MAJOR_BUMP":
                to_v = str(raw.extra.get("to_version") or raw.replacement_pattern or "")
                bump_pkg = str(raw.extra.get("package") or raw.affected_pattern or pkg)
                from_v = None
                for name, ver in ctx.installed.items():
                    if name.lower() == bump_pkg.lower() and ver:
                        from_v = ver
                        break
                from_v = from_v or str(raw.extra.get("from_version") or "")
                eco = packet_ecosystem_for(raw.extra.get("ecosystems") or [])
            signals.append(
                ChangeSignal(
                    source=f"module:{label}",
                    package=pkg,
                    change_type=event.change_type,
                    severity=event.severity,
                    from_version=from_v,
                    to_version=to_v,
                    ecosystem=eco,
                    affected_pattern=event.affected_pattern,
                    replacement_pattern=event.replacement_pattern,
                    description=event.description,
                    source_url=event.source_url,
                    deadline=event.deadline,
                    suggested_rules=list(rules),
                    hints={"event_id": event.event_id, "vendor": event.vendor},
                )
            )

    if profile.models_catalog_url or profile.model_doc_url_template:
        signals, compat_notes = apply_endpoint_compat(
            signals, client_state=client_state, demo=ctx.demo, profile=profile
        )
        if compat_notes:
            ctx.extra.setdefault("decision_notes", []).extend(compat_notes)
            for note in compat_notes:
                verbose_warnings.append(f"{label} endpoint compat: {note}")

    if profile.openapi_repo or profile.openapi_source_url:
        openapi_cache = ctx.extra.setdefault("openapi_pair_cache", {})
        signals, path_notes = apply_path_param_compat(
            signals,
            client_state=client_state,
            demo=ctx.demo,
            openapi_cache=openapi_cache,
            profile=profile,
        )
        if path_notes:
            ctx.extra.setdefault("decision_notes", []).extend(path_notes)
            for note in path_notes:
                verbose_warnings.append(f"{label} path param compat: {note}")

    if profile.name.lower() == "openai":
        signals, callee_notes = apply_sdk_callee_migration(
            signals,
            client_state=client_state,
            profile=profile,
        )
        if callee_notes:
            ctx.extra.setdefault("decision_notes", []).extend(callee_notes)
            for note in callee_notes:
                verbose_warnings.append(f"{label} sdk callee migration: {note}")

    return signals
