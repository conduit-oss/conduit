"""Packet enrich plugins: deterministic propose + guided LLM enrich.

OSS hero path stays ``packet new``; optional plugins fill rules and steer enrich
without requiring detect modules. See docs/migration-packets.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


PLUGIN_REASON_TAG = "plugin"
ENRICH_REASON_TAG = "enrich:research"


@dataclass
class EnrichGuide:
    """How a plugin steers ``synthesize_from_evidence``."""

    seed_urls: list[str] = field(default_factory=list)
    suggested_queries: list[str] = field(default_factory=list)
    allow_hosts: list[str] = field(default_factory=list)
    prompt_extra: str = ""
    context_chunks: list[str] = field(default_factory=list)


@dataclass
class ProposeResult:
    """Deterministic packet fills from ``PacketPlugin.propose`` (no LLM)."""

    rules: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    notes_append: str = ""


@runtime_checkable
class PacketPlugin(Protocol):
    """Optional library that fills and guides Migration Packet authoring."""

    name: str
    packages: list[str]

    def matches(self, package: str, ecosystem: str) -> bool:
        """Return True if this plugin handles ``package`` / ``ecosystem``."""

    def propose(
        self,
        *,
        package: str,
        ecosystem: str,
        from_version: str,
        to_version: str,
        base_packet: dict[str, Any],
        source_urls: list[str],
    ) -> ProposeResult:
        """Deterministic rules/sources/side_effects. May return empty."""

    def guide_enrich(
        self,
        *,
        package: str,
        ecosystem: str,
        from_version: str,
        to_version: str,
        packet: dict[str, Any],
        source_urls: list[str],
    ) -> EnrichGuide:
        """Seeds, queries, hosts, and prompt hints for enrich."""

    def after_enrich(
        self,
        *,
        packet: dict[str, Any],
        enrich_warnings: list[str],
    ) -> dict[str, Any]:
        """Validate / drop / annotate after enrich. Return the packet."""


class BasePacketPlugin:
    """Convenient base with default no-op propose/guide/after."""

    name: str = "plugin"
    packages: list[str] = []

    def matches(self, package: str, ecosystem: str) -> bool:
        del ecosystem
        want = (package or "").strip().lower()
        if not want:
            return False
        if self.name.lower() == want:
            return True
        return any(p.lower() == want for p in (self.packages or []))

    def propose(
        self,
        *,
        package: str,
        ecosystem: str,
        from_version: str,
        to_version: str,
        base_packet: dict[str, Any],
        source_urls: list[str],
    ) -> ProposeResult:
        del package, ecosystem, from_version, to_version, base_packet, source_urls
        return ProposeResult()

    def guide_enrich(
        self,
        *,
        package: str,
        ecosystem: str,
        from_version: str,
        to_version: str,
        packet: dict[str, Any],
        source_urls: list[str],
    ) -> EnrichGuide:
        del package, ecosystem, from_version, to_version, packet, source_urls
        return EnrichGuide()

    def after_enrich(
        self,
        *,
        packet: dict[str, Any],
        enrich_warnings: list[str],
    ) -> dict[str, Any]:
        del enrich_warnings
        return packet


def reason_tag(kind: str, name: str | None = None) -> str:
    """Build a provenance tag for rule ``reason`` (e.g. ``plugin:example-sdk``)."""
    kind = (kind or "").strip()
    name = (name or "").strip()
    if kind == PLUGIN_REASON_TAG and name:
        return f"{PLUGIN_REASON_TAG}:{name}"
    if kind == ENRICH_REASON_TAG:
        return ENRICH_REASON_TAG
    return kind or "plugin"


def stamp_rule_reason(rule: dict[str, Any], tag: str) -> dict[str, Any]:
    """Prefix ``reason`` with ``[tag]`` once."""
    if not isinstance(rule, dict):
        return rule
    tag = (tag or "").strip()
    if not tag:
        return rule
    prefix = f"[{tag}]"
    out = dict(rule)
    reason = str(out.get("reason") or "").strip()
    if reason.startswith(prefix):
        return out
    out["reason"] = f"{prefix} {reason}".strip() if reason else prefix
    return out


def stamp_rules(
    rules: list[dict[str, Any]] | None,
    tag: str,
) -> list[dict[str, Any]]:
    return [stamp_rule_reason(r, tag) for r in (rules or []) if isinstance(r, dict)]


def merge_propose_into_packet(
    packet: dict[str, Any],
    result: ProposeResult,
    *,
    plugin_name: str,
) -> dict[str, Any]:
    """Merge propose output into ``packet``; stamp plugin provenance on new rules."""
    from conduit.packet.synthesize import (
        merge_packet_rules,
        normalize_packet_side_effects,
        normalize_packet_sources,
    )

    tag = reason_tag(PLUGIN_REASON_TAG, plugin_name)
    out = dict(packet)
    proposed = stamp_rules(list(result.rules or []), tag)
    if proposed:
        out["rules"] = merge_packet_rules(list(out.get("rules") or []), proposed)
    if result.sources:
        out["sources"] = normalize_packet_sources(
            list(out.get("sources") or []) + list(result.sources)
        )
    if result.side_effects:
        existing = list(out.get("side_effects") or [])
        existing.extend(s for s in result.side_effects if isinstance(s, dict))
        out["side_effects"] = normalize_packet_side_effects(existing)
    note = (result.notes_append or "").strip()
    if note:
        prev = str(out.get("notes") or "").strip()
        out["notes"] = f"{prev}\n{note}".strip() if prev else note
    return out


def stamp_new_rules(
    before_rules: list[dict[str, Any]],
    after_packet: dict[str, Any],
    *,
    tag: str,
) -> dict[str, Any]:
    """Stamp rules that appear in ``after_packet`` but not in ``before_rules``."""
    from conduit.packet.from_detect import _rule_key

    before_keys = {
        _rule_key(r) for r in before_rules if isinstance(r, dict)
    }
    out = dict(after_packet)
    stamped: list[dict[str, Any]] = []
    for rule in out.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if _rule_key(rule) in before_keys:
            stamped.append(rule)
        else:
            stamped.append(stamp_rule_reason(rule, tag))
    out["rules"] = stamped
    return out


def drop_uncited_enrich_rewrites(
    packet: dict[str, Any],
    *,
    keep_plugin_prefix: str = f"[{PLUGIN_REASON_TAG}:",
) -> tuple[dict[str, Any], int]:
    """
    Drop enrich-stamped rewrite rules whose reason has no http(s) citation.

    Plugin-stamped rules are kept. DEPENDENCY_* rules are always kept.
    Returns (packet, dropped_count).
    """
    rewrite_types = {
        "EXACT_STRING_REPLACE",
        "REGEX_REPLACE",
        "AST_CALL_REWRITE",
        "AST_ATTR_REWRITE",
        "AST_ATTR_RENAME",
        "AST_PARAM_RENAME",
        "AST_PARAM_DROP",
        "AST_IMPORT_REWRITE",
        "KEY_RENAME",
    }
    enrich_prefix = f"[{ENRICH_REASON_TAG}]"
    kept: list[dict[str, Any]] = []
    dropped = 0
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "")
        reason = str(rule.get("reason") or "")
        if rtype.startswith("DEPENDENCY_") or keep_plugin_prefix in reason:
            kept.append(rule)
            continue
        if rtype in rewrite_types and reason.startswith(enrich_prefix):
            if "http://" not in reason and "https://" not in reason:
                dropped += 1
                continue
        kept.append(rule)
    out = dict(packet)
    out["rules"] = kept
    return out, dropped
