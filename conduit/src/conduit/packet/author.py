"""Guided packet authoring from source URLs (``conduit packet new``)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from conduit.context.fetch import fetch_url
from conduit.llm import get_llm_client
from conduit.packet.cache import save_packet
from conduit.packet.from_detect import find_previous_snapshot
from conduit.packet.synthesize import (
    collapse_dependency_bumps,
    empty_packet,
    synthesize_from_docs,
    synthesize_from_evidence,
)
from conduit.packet.validate import validate_packet

LogFn = Callable[[str], None]

_ANY_FROM = "*"


def _safe_slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-._")
    return text or "packet"


def packet_id_for_target(package: str, ecosystem: str, version: str) -> str:
    return (
        f"{_safe_slug(package)}-{_safe_slug(ecosystem)}-{_safe_slug(version)}"
    )


def default_packet_out_path(
    *,
    package: str,
    ecosystem: str,
    version: str | None = None,
    to_version: str | None = None,
    out_dir: Path | None = None,
) -> Path:
    ver = version or to_version or ""
    base = out_dir or Path("packets")
    return base / f"{packet_id_for_target(package, ecosystem, ver)}.json"


def guess_source_kind(url: str) -> str:
    text = (url or "").lower()
    if "changelog" in text or "release" in text or "releases" in text:
        if "github.com" in text and ("/releases" in text or "/tags" in text):
            return "github_release"
        return "changelog"
    if "github.com" in text and "/releases" in text:
        return "github_release"
    if any(tok in text for tok in ("migrate", "migration", "docs", "guide", "readme")):
        return "docs"
    if "openapi" in text or "swagger" in text:
        return "openapi"
    return "other"


def ecosystems_for_packet(ecosystem: str) -> list[str]:
    eco = (ecosystem or "pypi").strip().lower()
    if eco == "pypi":
        return ["pip", "pyproject"]
    if eco == "npm":
        return ["npm"]
    if eco == "go":
        return ["go"]
    if eco == "maven":
        return ["maven", "gradle"]
    return ["pip", "pyproject"]


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        url = str(src.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        kind = str(src.get("kind") or "other")
        out.append({"url": url, "kind": kind})
    return out


def _rule_key(rule: dict[str, Any]) -> str:
    from conduit.packet.from_detect import _rule_key as _key

    return _key(rule)


def summarize_rule(rule: dict[str, Any]) -> str:
    rtype = str(rule.get("type") or "?")
    if rtype == "DEPENDENCY_BUMP":
        return (
            f"{rtype} {rule.get('package')} "
            f"{rule.get('from_version')} -> {rule.get('to_version')}"
        )
    if rtype == "EXACT_STRING_REPLACE":
        match = rule.get("match") or rule.get("old")
        replace = rule.get("replace") or rule.get("new")
        return f"{rtype} {match!r} -> {replace!r}"
    if rtype in {"AST_CALL_REWRITE", "AST_ATTR_REWRITE", "AST_ATTR_RENAME"}:
        return (
            f"{rtype} {rule.get('old_callee') or rule.get('old_attr')} -> "
            f"{rule.get('new_callee') or rule.get('new_attr')}"
        )
    if rtype == "AST_PARAM_RENAME":
        return (
            f"{rtype} {rule.get('function_target')} "
            f"{rule.get('old_param')} -> {rule.get('new_param')}"
        )
    if rtype == "AST_IMPORT_REWRITE":
        return f"{rtype} {rule.get('old_import')} -> {rule.get('new_import')}"
    bits = [rtype]
    for key in ("old", "new", "old_callee", "new_callee", "package", "path"):
        if rule.get(key):
            bits.append(f"{key}={rule.get(key)}")
    return " ".join(bits)


def diff_packet_rules(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    cur_map: dict[str, dict[str, Any]] = {}
    for rule in current.get("rules") or []:
        if isinstance(rule, dict):
            cur_map[_rule_key(rule)] = rule
    prev_map: dict[str, dict[str, Any]] = {}
    for rule in (previous or {}).get("rules") or []:
        if isinstance(rule, dict):
            prev_map[_rule_key(rule)] = rule
    added = [cur_map[k] for k in cur_map if k not in prev_map]
    removed = [prev_map[k] for k in prev_map if k not in cur_map]
    return {"added": added, "removed": removed}


def load_previous_for_diff(
    packet: dict[str, Any],
    *,
    previous_path: Path | None,
    search_dir: Path | None,
) -> dict[str, Any] | None:
    import json

    if previous_path is not None:
        return json.loads(previous_path.read_text(encoding="utf-8"))
    pkg = str(packet.get("package") or "")
    eco = str(packet.get("ecosystem") or "")
    to_v = str(packet.get("to_version") or "")
    if not (pkg and eco and to_v and search_dir is not None):
        return None
    return find_previous_snapshot(
        search_dir, package=pkg, ecosystem=eco, to_version=to_v
    )


def create_packet_new(
    *,
    package: str,
    ecosystem: str,
    version: str,
    source_urls: list[str] | None = None,
    out: Path | None = None,
    enrich: bool = True,
    scaffold_only: bool = False,
    plugin: str | None = None,
    log: LogFn | None = None,
) -> tuple[Path, dict[str, Any], list[str]]:
    """
    Author a target-version packet from source URLs.

    Always writes a schema-valid packet with a DEPENDENCY_BUMP hop and recorded
    sources. Optional packet plugins run ``propose`` (no LLM) even under
    ``--scaffold-only``. When ``enrich`` and an LLM is configured, fills more
    rules from fetched docs / evidence seeds (plugin may guide that step).
    """
    from conduit.packet.plugin import (
        ENRICH_REASON_TAG,
        merge_propose_into_packet,
        stamp_new_rules,
    )
    from conduit.packet.plugin_discovery import PluginResolveError, resolve_plugin

    warnings: list[str] = []
    emit = log if callable(log) else None

    package = (package or "").strip()
    ecosystem = (ecosystem or "pypi").strip().lower() or "pypi"
    version = str(version or "").strip()
    if not package:
        raise ValueError("package is required")
    if not version:
        raise ValueError("version is required")

    urls = [u.strip() for u in (source_urls or []) if u and str(u).strip()]
    seen_u: set[str] = set()
    unique_urls: list[str] = []
    for u in urls:
        if u not in seen_u:
            seen_u.add(u)
            unique_urls.append(u)

    dest = out or default_packet_out_path(
        package=package, ecosystem=ecosystem, version=version
    )

    packet = empty_packet(
        package=package,
        ecosystem=ecosystem,
        from_version=_ANY_FROM,
        to_version=version,
        notes=(
            f"Authored for target {package}@{version} ({ecosystem}). "
            "from_version=* means any consumer pin; resolved at apply/run."
        ),
    )
    packet["packet_id"] = packet_id_for_target(package, ecosystem, version)
    packet["side_effects"] = []
    packet["rules"] = [
        {
            "type": "DEPENDENCY_BUMP",
            "package": package,
            "from_version": _ANY_FROM,
            "to_version": version,
            "ecosystems": ecosystems_for_packet(ecosystem),
            "reason": f"Pin {package} to target version {version}",
        }
    ]

    fetched_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    for url in unique_urls:
        kind = guess_source_kind(url)
        sources.append({"url": url, "kind": kind})
        try:
            body = fetch_url(url)
            if body and body.strip():
                fetched_parts.append(f"### Source: {url}\n\n{body.strip()}")
            elif emit:
                emit(f"Fetched empty body for {url}")
        except Exception as exc:
            warnings.append(f"Failed to fetch {url}: {exc}")
            if emit:
                emit(f"Warning: Failed to fetch {url}: {exc}")

    packet["sources"] = _dedupe_sources(sources)

    try:
        plug = resolve_plugin(package=package, ecosystem=ecosystem, plugin=plugin)
    except PluginResolveError as exc:
        raise ValueError(str(exc)) from exc

    if plug is not None:
        if emit:
            emit(f"Using packet plugin {plug.name}")
        warnings.append(f"Using packet plugin {plug.name}")
        try:
            proposed = plug.propose(
                package=package,
                ecosystem=ecosystem,
                from_version=_ANY_FROM,
                to_version=version,
                base_packet=packet,
                source_urls=unique_urls,
            )
            before_n = len(packet.get("rules") or [])
            packet = merge_propose_into_packet(packet, proposed, plugin_name=plug.name)
            added = max(0, len(packet.get("rules") or []) - before_n)
            if emit:
                emit(f"Plugin propose: {added} rule(s) merged")
        except Exception as exc:
            warnings.append(f"Plugin propose failed ({plug.name}): {exc}")
            if emit:
                emit(f"Warning: Plugin propose failed ({plug.name}): {exc}")

    llm_ready = get_llm_client() is not None
    # Enrich needs either user URLs or plugin guide seeds.
    do_enrich = enrich and not scaffold_only and llm_ready
    if enrich and not scaffold_only and not llm_ready:
        warnings.append(
            "LLM not configured; wrote dependency hop + sources "
            "(+ plugin propose if any); no invented rules"
        )
    if scaffold_only or not enrich:
        warnings.append(
            "Enrichment skipped (--scaffold-only / --no-enrich); "
            "plugin propose still runs unless --plugin none"
        )

    if do_enrich:
        seed_urls = list(unique_urls)
        suggested = [
            f"{package} migration guide {version}",
            f"{package} changelog breaking changes {version}",
        ]
        allow_hosts: list[str] | None = None
        prompt_extra = ""
        context_extra: list[str] = []
        if plug is not None:
            try:
                guide = plug.guide_enrich(
                    package=package,
                    ecosystem=ecosystem,
                    from_version=_ANY_FROM,
                    to_version=version,
                    packet=packet,
                    source_urls=unique_urls,
                )
                seed_urls = list(
                    dict.fromkeys([*seed_urls, *(guide.seed_urls or [])])
                )
                if guide.suggested_queries:
                    suggested = list(
                        dict.fromkeys([*suggested, *guide.suggested_queries])
                    )
                if guide.allow_hosts:
                    allow_hosts = list(guide.allow_hosts)
                prompt_extra = str(guide.prompt_extra or "")
                context_extra = list(guide.context_chunks or [])
            except Exception as exc:
                warnings.append(f"Plugin guide_enrich failed ({plug.name}): {exc}")

        if not seed_urls and not suggested:
            warnings.append("Enrichment skipped (no seed URLs or queries)")
        else:
            joined = "\n\n".join(fetched_parts)
            if emit and joined:
                emit("Synthesizing rules from fetched sources…")
            if joined:
                packet = synthesize_from_docs(
                    package=package,
                    from_version=_ANY_FROM,
                    to_version=version,
                    ecosystem=ecosystem,
                    changelog_text=joined,
                    docs_text="",
                    base=packet,
                    append_local_sources=False,
                )
                packet["packet_id"] = packet_id_for_target(
                    package, ecosystem, version
                )
                packet["from_version"] = _ANY_FROM
                packet["to_version"] = version

            if emit:
                emit("Evidence enrichment from source URLs…")
            before_rules = list(packet.get("rules") or [])
            packet, ev_warnings = synthesize_from_evidence(
                package=package,
                from_version=_ANY_FROM,
                to_version=version,
                ecosystem=ecosystem,
                signals=[],
                base=packet,
                seed_urls=seed_urls,
                suggested_queries=suggested,
                allow_hosts=allow_hosts,
                prompt_extra=prompt_extra or None,
                context_chunks_extra=context_extra or None,
                log=emit,
            )
            warnings.extend(ev_warnings)
            packet = stamp_new_rules(
                before_rules, packet, tag=ENRICH_REASON_TAG
            )
            packet["packet_id"] = packet_id_for_target(package, ecosystem, version)
            packet["from_version"] = _ANY_FROM
            packet["to_version"] = version

            if plug is not None:
                try:
                    packet = plug.after_enrich(
                        packet=packet, enrich_warnings=list(ev_warnings)
                    )
                except Exception as exc:
                    warnings.append(
                        f"Plugin after_enrich failed ({plug.name}): {exc}"
                    )

    packet["rules"] = collapse_dependency_bumps(
        list(packet.get("rules") or []),
        package=package,
        from_version=_ANY_FROM,
        to_version=version,
    )
    has_bump = any(
        isinstance(r, dict)
        and r.get("type") == "DEPENDENCY_BUMP"
        and str(r.get("package") or "").lower() == package.lower()
        for r in packet.get("rules") or []
    )
    if not has_bump:
        packet.setdefault("rules", []).insert(
            0,
            {
                "type": "DEPENDENCY_BUMP",
                "package": package,
                "from_version": _ANY_FROM,
                "to_version": version,
                "ecosystems": ecosystems_for_packet(ecosystem),
                "reason": f"Pin {package} to target version {version}",
            },
        )

    packet["sources"] = _dedupe_sources(list(packet.get("sources") or []) + sources)

    errs = validate_packet(packet)
    if errs:
        warnings.append(f"Schema warnings: {errs[:5]}")

    save_packet(dest, packet)
    return dest, packet, warnings


def format_packet_summary(packet: dict[str, Any]) -> str:
    """Human-readable summary for ``packet test`` / try-it."""
    lines = [
        f"packet_id: {packet.get('packet_id')}",
        f"package: {packet.get('package')} ({packet.get('ecosystem')})",
        (
            f"from_version: {packet.get('from_version')} "
            f"→ to_version: {packet.get('to_version')}"
        ),
        f"rules: {len(packet.get('rules') or [])}",
        f"sources: {len(packet.get('sources') or [])}",
        f"side_effects: {len(packet.get('side_effects') or [])}",
    ]
    notes = packet.get("notes")
    if notes:
        lines.append(f"notes: {str(notes).strip()[:240]}")
    for src in packet.get("sources") or []:
        if isinstance(src, dict) and src.get("url"):
            lines.append(f"  source[{src.get('kind') or 'other'}]: {src['url']}")
    for effect in packet.get("side_effects") or []:
        if isinstance(effect, dict):
            lines.append(
                f"  side_effect[{effect.get('kind') or 'other'}]: {effect.get('detail')}"
            )
    return "\n".join(lines)
