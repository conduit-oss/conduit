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
PIN_ONLY_MARKER = "pin_only=allow"
PIN_ONLY_WARNING = (
    "Pin-only packet: not a rules-bearing hop "
    f"(marker {PIN_ONLY_MARKER})"
)
_DEPENDENCY_RULE_PREFIX = "DEPENDENCY_"


class ThinEnrichError(ValueError):
    """Enrich ran with fetched sources but produced no call-site rewrite rules."""


def call_site_rewrite_rules(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Rules beyond the DEPENDENCY_* pin family (package-neutral)."""
    out: list[dict[str, Any]] = []
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "")
        if rtype.startswith(_DEPENDENCY_RULE_PREFIX):
            continue
        out.append(rule)
    return out


def _mark_pin_only(packet: dict[str, Any], *, reason: str) -> None:
    note = f"{PIN_ONLY_MARKER} ({reason})"
    existing = str(packet.get("notes") or "").strip()
    if PIN_ONLY_MARKER in existing:
        return
    packet["notes"] = f"{existing}\n{note}".strip() if existing else note


def _require_pin_only_marker(packet: dict[str, Any]) -> None:
    if PIN_ONLY_MARKER not in str(packet.get("notes") or ""):
        raise ThinEnrichError(
            f"allow-pin-only override requires {PIN_ONLY_MARKER} in packet notes"
        )


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
    allow_pin_only: bool = False,
    log: LogFn | None = None,
) -> tuple[Path, dict[str, Any], list[str]]:
    """
    Author a target-version packet from source URLs.

    Always writes a schema-valid packet with a DEPENDENCY_BUMP hop and recorded
    sources. When ``enrich`` and an LLM is configured, fills rules from fetched
    docs / evidence seeds. Never invents AST rules without an LLM.

    After enrich with at least one fetched source, refuses to write when the
    packet has no call-site rewrite rules beyond DEPENDENCY_* unless
    ``allow_pin_only`` is set (noisy: warning + ``pin_only=allow`` note).
    """
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
    fetched_urls: list[str] = []
    sources: list[dict[str, Any]] = []
    for url in unique_urls:
        kind = guess_source_kind(url)
        sources.append({"url": url, "kind": kind})
        try:
            body = fetch_url(url)
            if body and body.strip():
                fetched_parts.append(f"### Source: {url}\n\n{body.strip()}")
                fetched_urls.append(url)
            elif emit:
                emit(f"Fetched empty body for {url}")
        except Exception as exc:
            warnings.append(f"Failed to fetch {url}: {exc}")
            if emit:
                emit(f"Warning: Failed to fetch {url}: {exc}")

    packet["sources"] = _dedupe_sources(sources)

    llm_ready = get_llm_client() is not None
    do_enrich = bool(unique_urls) and enrich and not scaffold_only and llm_ready
    if unique_urls and enrich and not scaffold_only and not llm_ready:
        warnings.append(
            "LLM not configured; wrote dependency hop + sources only (no invented rules)"
        )
    deliberate_pin = bool(scaffold_only or not enrich)
    if deliberate_pin:
        warnings.append("Enrichment skipped (--scaffold-only / --no-enrich)")

    if do_enrich:
        joined = "\n\n".join(fetched_parts)
        if emit:
            emit("Synthesizing rules from fetched sources…")
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
        packet["packet_id"] = packet_id_for_target(package, ecosystem, version)
        packet["from_version"] = _ANY_FROM
        packet["to_version"] = version

        if emit:
            emit("Evidence enrichment from source URLs…")
        packet, ev_warnings = synthesize_from_evidence(
            package=package,
            from_version=_ANY_FROM,
            to_version=version,
            ecosystem=ecosystem,
            signals=[],
            base=packet,
            seed_urls=unique_urls,
            suggested_queries=[
                f"{package} migration guide {version}",
                f"{package} changelog breaking changes {version}",
            ],
            log=emit,
        )
        warnings.extend(ev_warnings)
        packet["packet_id"] = packet_id_for_target(package, ecosystem, version)
        packet["from_version"] = _ANY_FROM
        packet["to_version"] = version

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

    thin_after_enrich = (
        do_enrich and bool(fetched_urls) and not call_site_rewrite_rules(packet)
    )
    if thin_after_enrich and not allow_pin_only:
        src_list = ", ".join(fetched_urls)
        raise ThinEnrichError(
            "zero call-site rules after enrich with fetched sources: "
            f"{src_list}. Re-run with richer docs or pass --allow-pin-only "
            "for a deliberate pin-only hop."
        )
    if thin_after_enrich or deliberate_pin:
        reason = (
            "enrich produced dependency pin only"
            if thin_after_enrich
            else "scaffold-only / no-enrich deliberate pin hop"
        )
        _mark_pin_only(packet, reason=reason)
        _require_pin_only_marker(packet)
        if PIN_ONLY_WARNING not in warnings:
            warnings.append(PIN_ONLY_WARNING)

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
