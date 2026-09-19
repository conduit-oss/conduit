"""Build Migration Packets from signals, docs, or LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conduit.detect.models import ChangeSignal
from conduit.packet.cache import save_packet
from conduit.packet.validate import validate_packet

_PLACEHOLDER_FROM = "0.0.0"
_PLACEHOLDER_TO = "1.0.0"

SOURCE_KINDS = frozenset({"github_release", "changelog", "docs", "openapi", "other"})
SIDE_EFFECT_KINDS = frozenset({"webhook", "database", "config", "other"})

_SOURCE_KIND_ALIASES = {
    "documentation": "docs",
    "document": "docs",
    "doc": "docs",
    "guide": "docs",
    "migration": "docs",
    "readme": "docs",
    "manual": "docs",
    "reference": "docs",
    "repository": "other",
    "repo": "other",
    "source": "other",
    "code": "other",
    "git": "other",
    "webpage": "other",
    "website": "other",
    "url": "other",
    "link": "other",
    "page": "other",
    "release": "github_release",
    "releases": "github_release",
    "tag": "github_release",
    "tags": "github_release",
    "github": "github_release",
    "github_release_notes": "github_release",
    "release_notes": "github_release",
    "changelog": "changelog",
    "changes": "changelog",
    "history": "changelog",
    "change_log": "changelog",
    "openapi": "openapi",
    "swagger": "openapi",
    "spec": "openapi",
    "api_spec": "openapi",
}

_SIDE_EFFECT_KIND_ALIASES = {
    "db": "database",
    "database": "database",
    "datastore": "database",
    "env": "config",
    "environment": "config",
    "configuration": "config",
    "settings": "config",
    "config": "config",
    "webhook": "webhook",
    "hooks": "webhook",
    "callback": "webhook",
    "other": "other",
    "manual": "other",
    "ops": "other",
}


def normalize_source_kind(kind: str | None) -> str:
    """Map LLM synonyms onto the schema ``sources[].kind`` enum."""
    raw = (kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return "other"
    if raw in SOURCE_KINDS:
        return raw
    if raw in _SOURCE_KIND_ALIASES:
        return _SOURCE_KIND_ALIASES[raw]
    if "changelog" in raw or raw.endswith("_changes") or "change_log" in raw:
        return "changelog"
    if "openapi" in raw or "swagger" in raw:
        return "openapi"
    if "github" in raw and ("release" in raw or "tag" in raw):
        return "github_release"
    if "release" in raw or raw.endswith("_tag"):
        return "github_release"
    if any(
        tok in raw
        for tok in ("doc", "guide", "migrate", "migration", "readme", "manual")
    ):
        return "docs"
    if any(tok in raw for tok in ("repo", "repository", "source", "git")):
        return "other"
    return "other"


def normalize_side_effect_kind(kind: str | None) -> str:
    """Map LLM synonyms onto the schema ``side_effects[].kind`` enum."""
    raw = (kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return "other"
    if raw in SIDE_EFFECT_KINDS:
        return raw
    if raw in _SIDE_EFFECT_KIND_ALIASES:
        return _SIDE_EFFECT_KIND_ALIASES[raw]
    if "webhook" in raw or "hook" in raw or "callback" in raw:
        return "webhook"
    if "database" in raw or raw == "db" or "datastore" in raw:
        return "database"
    if any(tok in raw for tok in ("config", "env", "setting")):
        return "config"
    return "other"


def normalize_packet_sources(sources: Any) -> list[dict[str, Any]]:
    """Rewrite ``sources[].kind`` to schema-valid values; drop malformed rows."""
    out: list[dict[str, Any]] = []
    if not isinstance(sources, list):
        return out
    for src in sources:
        if not isinstance(src, dict) or not src.get("url"):
            continue
        out.append(
            {
                "url": str(src["url"]),
                "kind": normalize_source_kind(src.get("kind")),
            }
        )
    return out


def normalize_packet_side_effects(side_effects: Any) -> list[dict[str, Any]]:
    """Rewrite ``side_effects[].kind`` to schema-valid values; drop malformed rows."""
    out: list[dict[str, Any]] = []
    if not isinstance(side_effects, list):
        return out
    for effect in side_effects:
        if not isinstance(effect, dict):
            continue
        detail = str(effect.get("detail") or "").strip()
        if not detail:
            continue
        out.append(
            {
                "kind": normalize_side_effect_kind(effect.get("kind")),
                "detail": detail,
            }
        )
    return out


# LLM often emits short aliases (old/new/scope) instead of schema field names.
_DEFAULT_TARGET_FILES = ["*.py"]
_LLM_RULE_FIELD_ALIASES: dict[str, tuple[str, str]] = {
    # rtype -> (old_key, new_key) when only old/new were supplied
    "AST_CALL_REWRITE": ("old_callee", "new_callee"),
    "AST_ATTR_RENAME": ("old_attr", "new_attr"),
    "AST_PARAM_RENAME": ("old_param", "new_param"),
    "AST_IMPORT_REWRITE": ("old_import", "new_import"),
    "KEY_RENAME": ("old_key", "new_key"),
}
_LLM_RULE_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "AST_CALL_REWRITE": frozenset(
        {"type", "target_files", "old_callee", "new_callee", "reason"}
    ),
    "AST_ATTR_RENAME": frozenset(
        {"type", "target_files", "old_attr", "new_attr", "reason"}
    ),
    "AST_PARAM_RENAME": frozenset(
        {
            "type",
            "target_files",
            "function_target",
            "old_param",
            "new_param",
            "reason",
        }
    ),
    "AST_PARAM_DROP": frozenset(
        {
            "type",
            "target_files",
            "function_target",
            "param",
            "values",
            "reason",
        }
    ),
    "AST_IMPORT_REWRITE": frozenset(
        {"type", "target_files", "old_import", "new_import", "reason"}
    ),
    "KEY_RENAME": frozenset(
        {"type", "target_files", "old_key", "new_key", "reason"}
    ),
    "EXACT_STRING_REPLACE": frozenset(
        {"type", "target_files", "match", "replace", "reason"}
    ),
    "REGEX_REPLACE": frozenset(
        {"type", "target_files", "pattern", "replace", "reason"}
    ),
    "DEPENDENCY_BUMP": frozenset(
        {
            "type",
            "package",
            "from_version",
            "to_version",
            "ecosystems",
            "scope",
            "reason",
        }
    ),
    "DEPENDENCY_ADD": frozenset(
        {
            "type",
            "package",
            "to_version",
            "ecosystems",
            "scope",
            "reason",
        }
    ),
    "DEPENDENCY_REMOVE": frozenset(
        {
            "type",
            "package",
            "from_version",
            "ecosystems",
            "scope",
            "reason",
        }
    ),
}


def normalize_llm_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Map common LLM aliases onto schema field names; strip unknown keys."""
    if not isinstance(rule, dict):
        return rule
    rtype = str(rule.get("type") or "").strip()
    out = dict(rule)

    pair = _LLM_RULE_FIELD_ALIASES.get(rtype)
    if pair:
        old_key, new_key = pair
        if not out.get(old_key) and out.get("old") is not None:
            out[old_key] = out["old"]
        if not out.get(new_key) and out.get("new") is not None:
            out[new_key] = out["new"]

    if rtype == "AST_PARAM_DROP" and not out.get("param") and out.get("old") is not None:
        out["param"] = out["old"]

    if rtype in {"AST_PARAM_RENAME", "AST_PARAM_DROP"}:
        if not out.get("function_target"):
            scope = out.get("scope") or out.get("function") or out.get("target")
            if scope:
                out["function_target"] = str(scope)

    needs_targets = rtype in {
        "AST_CALL_REWRITE",
        "AST_ATTR_RENAME",
        "AST_PARAM_RENAME",
        "AST_PARAM_DROP",
        "AST_IMPORT_REWRITE",
        "KEY_RENAME",
        "EXACT_STRING_REPLACE",
        "REGEX_REPLACE",
    }
    if needs_targets:
        targets = out.get("target_files")
        if not isinstance(targets, list) or not targets:
            out["target_files"] = list(_DEFAULT_TARGET_FILES)

    allowed = _LLM_RULE_ALLOWED_KEYS.get(rtype)
    if allowed is not None:
        out = {k: v for k, v in out.items() if k in allowed}
    return out


def normalize_llm_rules(rules: Any) -> list[dict[str, Any]]:
    """Normalize a rules list from LLM JSON; drop non-dict rows."""
    if not isinstance(rules, list):
        return []
    return [
        normalize_llm_rule(r) for r in rules if isinstance(r, dict) and r.get("type")
    ]


def _parse_ver(value: str | None):
    if not value:
        return None
    try:
        from packaging.version import Version

        return Version(str(value).lstrip("v"))
    except Exception:
        return None


@dataclass
class PacketEnsureResult:
    packet: dict[str, Any]
    from_source: str = "placeholder"  # file | cache | signal | source | manifest | rule | fixture | placeholder
    to_source: str = "placeholder"
    used_fixture: bool = False
    warnings: list[str] = field(default_factory=list)


def _installed_version(installed: dict[str, str] | None, package: str) -> str | None:
    if not installed:
        return None
    want = package.lower()
    for name, ver in installed.items():
        if name.lower() == want and ver:
            return str(ver)
    return None


def _to_version_from_rules(rules: list[dict[str, Any]], package: str) -> str | None:
    want = package.lower()
    for rule in rules:
        if str(rule.get("type") or "") != "DEPENDENCY_BUMP":
            continue
        pkg = str(rule.get("package") or "").lower()
        if pkg and pkg != want:
            continue
        to_v = rule.get("to_version")
        if to_v:
            return str(to_v)
    return None


def _source_usage_index(source: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize source packet tokens for in-scope filtering."""
    models: set[str] = set()
    paths: set[str] = set()
    callees: set[str] = set()
    if not source:
        return {"models": models, "paths": paths, "callees": callees, "has_usage": False}
    from conduit.detect.modules.openai.path_callees import (
        normalize_api_path,
        path_for_api_pattern,
    )

    for mid in source.get("model_ids") or []:
        if mid:
            models.add(str(mid).lower())
    for raw in source.get("api_patterns") or []:
        token = str(raw or "").strip()
        if not token:
            continue
        mapped = path_for_api_pattern(token) or (
            normalize_api_path(token if token.startswith("/") else f"/{token}")
        )
        if mapped:
            paths.add(mapped.lower())
        if not token.startswith("/"):
            callees.add(token.lower())
    for usage in source.get("usages") or []:
        if not isinstance(usage, dict):
            continue
        ident = str(usage.get("id") or "").strip()
        if ident:
            models.add(ident.lower())
        for callee in usage.get("callees") or []:
            if callee:
                callees.add(str(callee).lower())
                mapped = path_for_api_pattern(str(callee))
                if mapped:
                    paths.add(mapped.lower())
        for path in usage.get("paths") or []:
            mapped = path_for_api_pattern(str(path)) or normalize_api_path(
                str(path) if str(path).startswith("/") else f"/{path}"
            )
            if mapped:
                paths.add(mapped.lower())
    return {
        "models": models,
        "paths": paths,
        "callees": callees,
        "has_usage": bool(models or paths or callees),
    }


def _signal_in_scope(signal: ChangeSignal, index: dict[str, Any]) -> bool:
    if signal.change_type in {
        "DEPENDENCY_BUMP",
        "SDK_MAJOR_BUMP",
        "SDK_BUMP",
        "PARAM_RENAME",
        "PARAM_REMOVED",
        "SDK_CALLEE_MIGRATION",
        "PACKAGE_ADDED",
        "PACKAGE_REMOVED",
    }:
        return True
    from conduit.detect.modules.openai.path_callees import normalize_api_path

    aff = (signal.affected_pattern or "").strip()
    repl = (signal.replacement_pattern or "").strip()
    for token in (aff, repl):
        if not token:
            continue
        low = token.lower()
        if low in index["models"] or low in index["callees"]:
            return True
        mapped = normalize_api_path(token if token.startswith("/") else f"/{token}")
        if mapped and mapped.lower() in index["paths"]:
            return True
    hints = signal.hints or {}
    for key in ("path", "old_path", "new_path"):
        mapped = normalize_api_path(str(hints.get(key) or "") or None)
        if mapped and mapped.lower() in index["paths"]:
            return True
    return False


def filter_signals_to_source(
    signals: list[ChangeSignal],
    source: dict[str, Any] | None,
    *,
    package: str,
) -> list[ChangeSignal]:
    """Drop vendor catalog signals that do not touch scanned client usage."""
    index = _source_usage_index(source)
    if not index["has_usage"]:
        return list(signals)
    out: list[ChangeSignal] = []
    for signal in signals:
        if signal.package.lower() != package.lower():
            out.append(signal)
            continue
        if _signal_in_scope(signal, index):
            out.append(signal)
    return out


_DEP_RULE_TYPES = frozenset(
    {"DEPENDENCY_BUMP", "DEPENDENCY_ADD", "DEPENDENCY_REMOVE"}
)


def _named_dep_packages(rules: list[dict[str, Any]], package: str) -> set[str]:
    named = {package.lower()}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") not in _DEP_RULE_TYPES:
            continue
        pkg = str(rule.get("package") or "").strip().lower()
        if pkg:
            named.add(pkg)
    return named


def fold_companion_rules(
    rules: list[dict[str, Any]],
    signals: list[ChangeSignal],
    *,
    package: str,
) -> list[dict[str, Any]]:
    """Append suggested_rules from signals whose package is already named."""
    out = list(rules)
    seen = {json.dumps(r, sort_keys=True) for r in out if isinstance(r, dict)}
    named = _named_dep_packages(out, package)
    changed = True
    while changed:
        changed = False
        for signal in signals:
            if signal.package.lower() not in named:
                continue
            for rule in signal.suggested_rules:
                if not isinstance(rule, dict):
                    continue
                item = dict(rule)
                key = json.dumps(item, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
                pkg = str(item.get("package") or "").strip().lower()
                if (
                    str(item.get("type") or "") in _DEP_RULE_TYPES
                    and pkg
                    and pkg not in named
                ):
                    named.add(pkg)
                    changed = True
    return out


def collapse_dependency_bumps(
    rules: list[dict[str, Any]],
    *,
    package: str,
    from_version: str,
    to_version: str,
) -> list[dict[str, Any]]:
    """Keep a single DEPENDENCY_BUMP per package, pinned to packet from/to."""
    want = package.lower()
    out: list[dict[str, Any]] = []
    bump: dict[str, Any] | None = None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "DEPENDENCY_BUMP":
            out.append(rule)
            continue
        pkg = str(rule.get("package") or want).lower()
        if pkg != want:
            out.append(rule)
            continue
        bump = dict(rule)
    if bump and from_version and to_version and from_version != to_version:
        bump["package"] = package
        bump["from_version"] = from_version
        bump["to_version"] = to_version
        out.insert(0, bump)
    return out


def _apply_versions(
    packet: dict[str, Any],
    *,
    package: str,
    from_version: str,
    to_version: str,
) -> None:
    packet["package"] = package
    packet["from_version"] = from_version
    packet["to_version"] = to_version
    packet["packet_id"] = packet_id_for(package, from_version, to_version)


def packet_id_for(package: str, from_version: str, to_version: str) -> str:
    return f"{package}-{from_version}-{to_version}"


def empty_packet(
    *,
    package: str,
    ecosystem: str,
    from_version: str,
    to_version: str,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "packet_id": packet_id_for(package, from_version, to_version),
        "package": package,
        "ecosystem": ecosystem,
        "from_version": from_version,
        "to_version": to_version,
        "sources": [],
        "notes": notes,
        "rules": [],
    }


def packet_from_signals(
    signals: list[ChangeSignal],
    *,
    package: str,
    ecosystem: str = "pypi",
    from_version: str = "0.0.0",
    to_version: str = "1.0.0",
    companion_signals: list[ChangeSignal] | None = None,
) -> dict[str, Any]:
    """Assemble a packet from ChangeSignal suggested_rules (deterministic)."""
    pkg_signals = [s for s in signals if s.package.lower() == package.lower()]
    placeholder_from = from_version in {_PLACEHOLDER_FROM, "", None}
    placeholder_to = to_version in {_PLACEHOLDER_TO, "", None}
    if pkg_signals and (placeholder_from or placeholder_to):
        for s in pkg_signals:
            if s.change_type not in {"SDK_MAJOR_BUMP", "DEPENDENCY_BUMP", "SDK_BUMP"}:
                continue
            if placeholder_from and s.from_version:
                from_version = s.from_version
                placeholder_from = False
            if placeholder_to and s.to_version:
                to_version = s.to_version
                placeholder_to = False
            if s.ecosystem:
                ecosystem = s.ecosystem
            if not placeholder_from and not placeholder_to:
                break

    decision_notes: list[str] = []
    packet = empty_packet(
        package=package,
        ecosystem=ecosystem if ecosystem in {"pypi", "npm", "go", "maven", "other"} else "other",
        from_version=from_version,
        to_version=to_version,
        notes="Synthesized from detect signals",
    )
    rules: list[dict[str, Any]] = []
    sources: list[dict[str, str]] = []
    seen_rules: set[str] = set()
    seen_sources: set[str] = set()
    for s in pkg_signals or signals:
        if s.source_url and s.source_url not in seen_sources:
            sources.append({"url": s.source_url, "kind": "other"})
            seen_sources.add(s.source_url)
        hint_reason = None
        if isinstance(s.hints, dict):
            hint_reason = s.hints.get("endpoint_compat_reason")
        if s.change_type in {"MODEL_DEPRECATION", "MODEL_REMOVED"} and (
            s.description or hint_reason
        ):
            decision_notes.append(str(hint_reason or s.description))
        for rule in s.suggested_rules:
            rule = dict(rule)
            if not rule.get("reason") and (s.description or hint_reason):
                rule["reason"] = str(hint_reason or s.description)
            key = json.dumps(rule, sort_keys=True)
            if key in seen_rules:
                continue
            seen_rules.add(key)
            rules.append(rule)
        # Model deprecations without suggested rules still become string replaces
        if (
            s.change_type in {"MODEL_DEPRECATION", "MODEL_REMOVED"}
            and s.affected_pattern
            and s.replacement_pattern
        ):
            reason = str(
                hint_reason
                or s.description
                or (
                    f"Replace deprecated/removed model {s.affected_pattern} "
                    f"with {s.replacement_pattern}."
                )
            )
            rule = {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py", "*.ts", "*.js", "*.yaml", "*.yml", "*.json", ".env*"],
                "match": s.affected_pattern,
                "replace": s.replacement_pattern,
                "reason": reason,
            }
            key = json.dumps(rule, sort_keys=True)
            if key not in seen_rules:
                seen_rules.add(key)
                rules.append(rule)
    packet["rules"] = collapse_dependency_bumps(
        fold_companion_rules(
            rules,
            list(companion_signals or []) + list(signals),
            package=package,
        ),
        package=package,
        from_version=str(from_version),
        to_version=str(to_version),
    )
    packet["sources"] = sources
    if decision_notes:
        # Deduplicate while preserving order
        uniq: list[str] = []
        seen_n: set[str] = set()
        for note in decision_notes:
            if note in seen_n:
                continue
            seen_n.add(note)
            uniq.append(note)
        base_notes = str(packet.get("notes") or "").strip()
        joined = "\n".join(f"- {n}" for n in uniq)
        packet["notes"] = (
            f"{base_notes}\n\nDecision rationale:\n{joined}".strip()
            if base_notes
            else f"Decision rationale:\n{joined}"
        )
    return packet


def synthesize_from_docs(
    *,
    package: str,
    from_version: str,
    to_version: str,
    ecosystem: str = "pypi",
    changelog_text: str = "",
    docs_text: str = "",
    base: dict[str, Any] | None = None,
    append_local_sources: bool = True,
) -> dict[str, Any]:
    """
    Build a packet from vendor docs. Uses configured LLM when available;
    otherwise returns base packet or empty rules with sources noted.
    """
    from conduit.llm import get_llm_client

    packet = base or empty_packet(
        package=package,
        ecosystem=ecosystem,
        from_version=from_version,
        to_version=to_version,
        notes="Synthesized from vendor docs",
    )
    if append_local_sources:
        if changelog_text:
            packet.setdefault("sources", []).append(
                {"url": "local://changelog", "kind": "changelog"}
            )
        if docs_text:
            packet.setdefault("sources", []).append(
                {"url": "local://docs", "kind": "docs"}
            )

    client = get_llm_client()
    if client is None or not (changelog_text or docs_text):
        return packet

    prompt = {
        "instructions": (
            "Generate a Conduit migration packet JSON with keys: "
            "packet_id, package, ecosystem, from_version, to_version, sources, notes, "
            "side_effects, rules. "
            "Rules may use EXACT_STRING_REPLACE, REGEX_REPLACE, AST_PARAM_RENAME, "
            "DEPENDENCY_BUMP, AST_IMPORT_REWRITE, AST_ATTR_RENAME, AST_CALL_REWRITE. "
            "AST_CALL_REWRITE requires target_files, old_callee, new_callee "
            "(never old/new). "
            "AST_ATTR_RENAME requires target_files, old_attr, new_attr. "
            "AST_PARAM_RENAME requires target_files, function_target, old_param, "
            "new_param. "
            "AST_IMPORT_REWRITE requires target_files, old_import, new_import. "
            "Use target_files=['*.py'] when unsure. Do not emit scope/arguments/"
            "old/new aliases. "
            "sources[].kind MUST be exactly one of: github_release, changelog, docs, "
            "openapi, other. Never use synonyms (documentation, repository, repo, "
            "guide, release, webpage). "
            "side_effects[].kind MUST be exactly one of: webhook, database, config, other. "
            "Never use synonyms (db, env, configuration). "
            "Only propose replacements grounded in the provided changelog/docs. "
            "If a successor is unknown, put it in notes — do not invent paths or callees. "
            "Multi-statement API gaps and non-code ripples (webhooks, DBs, config) go in "
            "side_effects as {kind, detail} — never invent fake string rewrites for them. "
            "Reply with JSON only."
        ),
        "package": package,
        "from_version": from_version,
        "to_version": to_version,
        "ecosystem": ecosystem,
        "changelog": changelog_text[:12000],
        "docs": docs_text[:12000],
        "seed": packet,
    }
    try:
        data = client.complete_json(
            system=(
                "You author Conduit migration packets. JSON only. "
                "Never invent API successors. Put uncodemodable gaps in side_effects. "
                "Rule fields must match schema names "
                "(old_callee/new_callee, old_attr/new_attr, old_param/new_param) "
                "plus target_files. "
                "sources[].kind must be exactly github_release|changelog|docs|openapi|other. "
                "side_effects[].kind must be exactly webhook|database|config|other."
            ),
            user=json.dumps(prompt),
        )
        if data and isinstance(data, dict):
            if "sources" in data:
                data["sources"] = normalize_packet_sources(data.get("sources"))
            if "side_effects" in data:
                data["side_effects"] = normalize_packet_side_effects(
                    data.get("side_effects")
                )
            if "rules" in data:
                data["rules"] = normalize_llm_rules(data.get("rules"))
            # Keep seed identity fields so validate_packet can succeed.
            for key in (
                "packet_id",
                "package",
                "ecosystem",
                "from_version",
                "to_version",
            ):
                if not data.get(key) and packet.get(key) is not None:
                    data[key] = packet[key]
            if not validate_packet(data):
                return data
    except Exception:
        pass
    return packet


_EVIDENCE_SYSTEM = (
    "You are a Staff Software Engineer authoring Conduit Migration Packets. "
    "Emit JSON only with keys: notes (string), sources (list of {url, kind}), "
    "side_effects (list of {kind, detail}), rules (list). "
    "Allowed rule types: EXACT_STRING_REPLACE, REGEX_REPLACE, AST_PARAM_RENAME, "
    "DEPENDENCY_BUMP, AST_IMPORT_REWRITE, AST_ATTR_RENAME, AST_CALL_REWRITE. "
    "AST_CALL_REWRITE fields: target_files, old_callee, new_callee (never old/new). "
    "AST_ATTR_RENAME fields: target_files, old_attr, new_attr. "
    "AST_PARAM_RENAME fields: target_files, function_target, old_param, new_param. "
    "AST_IMPORT_REWRITE fields: target_files, old_import, new_import. "
    "Default target_files to ['*.py']. Do not emit scope/arguments aliases. "
    "sources[].kind MUST be exactly one of: github_release, changelog, docs, "
    "openapi, other. Never use synonyms (documentation, repository, repo, guide, "
    "release, webpage). "
    "side_effects[].kind MUST be exactly one of: webhook, database, config, other. "
    "Never use synonyms (db, env, configuration). "
    "Every path replace, param rename, and call rewrite MUST be supported by the evidence "
    "excerpts (cite URLs in notes). "
    "For AST_PARAM_RENAME include explicit function_target(s) taken from evidence — "
    "do not assume ChatCompletion vs chat.completions. "
    "For AST_PARAM_DROP include function_target, param, and optional values "
    "(literal kwargs to omit). "
    "If a removed endpoint/param has no stated successor, mention it in notes and do NOT "
    "invent replace/new_callee/new_param. "
    "Multi-statement API gaps and non-code ripples go in side_effects — never invent "
    "fake string rewrites for them. "
    "Do not invent model ids. "
    "When proposing a model EXACT_STRING_REPLACE, the replacement must support the client "
    "endpoints implied by detect_signals / evidence (see model Supported endpoints tables). "
    "Every rule MUST include a short 'reason' string explaining why it was chosen "
    "(cite the source URL). "
    "Honor any ignore list: do not emit rules whose only effect would be rewriting "
    "ignored contract patterns/files (LEGACY_/FORBIDDEN_ oracles). "
    "Scope rules to the provided source packet: only models/callees/paths the client "
    "uses. Prefer AST_CALL_REWRITE / AST_ATTR_RENAME for SDK call surfaces observed "
    "in source.usages (path-string replaces are not enough when the client calls "
    "Resource.create). Use KEY_RENAME when request/response dict keys, JSON/YAML "
    "fixtures, or .env names change (AST_PARAM_RENAME only rewrites call kwargs). "
    "One primary DEPENDENCY_BUMP pinned to packet from_version → to_version. "
    "When evidence names companion packages (splits, extra wheels), emit "
    "DEPENDENCY_ADD / DEPENDENCY_REMOVE / extra DEPENDENCY_BUMP with ecosystems "
    "and scope (main|dev|peer) — do not invent companion names. "
    "Cover every in-scope deprecated usage; if a successor is documented, emit a rule."
)


def _rule_dedupe_key(rule: dict[str, Any]) -> str:
    rtype = str(rule.get("type") or "")
    if rtype == "EXACT_STRING_REPLACE":
        return json.dumps(
            {"type": rtype, "match": rule.get("match"), "replace": rule.get("replace")},
            sort_keys=True,
        )
    if rtype == "AST_PARAM_RENAME":
        return json.dumps(
            {
                "type": rtype,
                "function_target": rule.get("function_target"),
                "old_param": rule.get("old_param"),
                "new_param": rule.get("new_param"),
            },
            sort_keys=True,
        )
    if rtype == "AST_PARAM_DROP":
        return json.dumps(
            {
                "type": rtype,
                "function_target": rule.get("function_target"),
                "param": rule.get("param") or rule.get("old_param"),
                "values": rule.get("values"),
            },
            sort_keys=True,
        )
    if rtype == "AST_CALL_REWRITE":
        return json.dumps(
            {
                "type": rtype,
                "old_callee": rule.get("old_callee"),
                "new_callee": rule.get("new_callee"),
            },
            sort_keys=True,
        )
    if rtype == "AST_ATTR_RENAME":
        return json.dumps(
            {
                "type": rtype,
                "old_attr": rule.get("old_attr"),
                "new_attr": rule.get("new_attr"),
            },
            sort_keys=True,
        )
    if rtype == "KEY_RENAME":
        return json.dumps(
            {
                "type": rtype,
                "old_key": rule.get("old_key"),
                "new_key": rule.get("new_key"),
            },
            sort_keys=True,
        )
    if rtype in {"DEPENDENCY_ADD", "DEPENDENCY_REMOVE", "DEPENDENCY_BUMP"}:
        return json.dumps(
            {
                "type": rtype,
                "package": rule.get("package"),
                "from_version": rule.get("from_version"),
                "to_version": rule.get("to_version"),
                "scope": rule.get("scope") or "main",
            },
            sort_keys=True,
        )
    return json.dumps(rule, sort_keys=True)


def merge_packet_rules(
    base_rules: list[dict[str, Any]],
    llm_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep grounded base rules; append LLM rules; scrape EXACT match wins on conflict."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    exact_matches: set[str] = set()
    for rule in base_rules:
        key = _rule_dedupe_key(rule)
        if key in seen:
            continue
        seen.add(key)
        out.append(rule)
        if rule.get("type") == "EXACT_STRING_REPLACE" and rule.get("match"):
            exact_matches.add(str(rule["match"]))
    for rule in llm_rules:
        if (
            rule.get("type") == "EXACT_STRING_REPLACE"
            and rule.get("match")
            and str(rule["match"]) in exact_matches
        ):
            continue
        key = _rule_dedupe_key(rule)
        if key in seen:
            continue
        seen.add(key)
        out.append(rule)
    return out


def _signal_summary(signals: list[ChangeSignal], package: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in signals:
        if s.package.lower() != package.lower():
            continue
        rows.append(
            {
                "change_type": s.change_type,
                "affected_pattern": s.affected_pattern,
                "replacement_pattern": s.replacement_pattern,
                "description": s.description,
                "source_url": s.source_url,
                "suggested_rule_count": len(s.suggested_rules),
            }
        )
    return rows[:200]


def _module_evidence_meta(
    package: str, from_version: str, to_version: str
) -> tuple[list[str], list[str], list[str]]:
    from conduit.detect.modules.discovery import load_modules

    for mod in load_modules():
        pkgs = {x.lower() for x in mod.packages}
        if package.lower() in pkgs or mod.name.lower() == package.lower():
            return (
                list(mod.evidence_seeds()),
                list(mod.evidence_hosts()),
                list(
                    mod.evidence_queries(
                        from_version=from_version, to_version=to_version
                    )
                ),
            )
    return [], [], []


def synthesize_from_evidence(
    *,
    package: str,
    from_version: str,
    to_version: str,
    ecosystem: str,
    signals: list[ChangeSignal],
    base: dict[str, Any],
    root: Path | None = None,
    source_packet: dict[str, Any] | None = None,
    missed_items: list[dict[str, Any]] | None = None,
    publisher: bool = False,
    log: Any | None = None,
    seed_urls: list[str] | None = None,
    suggested_queries: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    LLM-author rules via Responses agent tools (web_search / fetch_url / read_file).
    Returns (packet, warnings).

    When ``seed_urls`` / ``suggested_queries`` are provided, they override detect-module
    evidence metadata (so authoring from links works without a vendor module).
    """
    from conduit.llm import attach_llm_log, get_llm_client
    from conduit.llm.executors import RepoToolExecutor
    from conduit.llm.tools import agent_tools
    from conduit.repair_ignore import IgnoreList, build_ignore_list
    from urllib.parse import urlparse

    warnings: list[str] = []
    emit = log if callable(log) else None
    client = attach_llm_log(get_llm_client(), emit)
    if client is None:
        warnings.append("LLM packet enrichment skipped (no LLM configured)")
        return base, warnings

    mod_seeds, mod_hosts, mod_queries = _module_evidence_meta(
        package, from_version, to_version
    )
    seeds = list(seed_urls) if seed_urls is not None else list(mod_seeds)
    queries = (
        list(suggested_queries) if suggested_queries is not None else list(mod_queries)
    )
    hosts = list(mod_hosts)
    if seed_urls is not None:
        for url in seeds:
            host = (urlparse(url).hostname or "").lower()
            if host and host not in hosts:
                hosts.append(host)
    if not seeds and not queries:
        warnings.append(
            f"LLM packet enrichment skipped (no evidence seeds for package {package!r})"
        )
        return base, warnings

    profile = None
    try:
        from conduit.detect.vendor_profile import profile_for_package

        profile = profile_for_package(package)
    except Exception:
        profile = None

    ignore = IgnoreList()
    ignore_payload: dict[str, Any] = {}
    if root is not None:
        ignore = build_ignore_list(root, base)
        ignore_payload = ignore.to_prompt_dict()

    scoped_signals = (
        list(signals)
        if publisher
        else filter_signals_to_source(signals, source_packet, package=package)
    )
    has_source_usage = bool(_source_usage_index(source_packet)["has_usage"])

    context_chunks: list[str] = [
        f"{s.change_type} {s.affected_pattern} {s.replacement_pattern} {s.description}"
        for s in scoped_signals
        if s.package.lower() == package.lower()
    ]
    if missed_items:
        context_chunks.extend(
            f"{i.get('kind')} {i.get('value')} {i.get('detail')}"
            for i in missed_items
            if isinstance(i, dict)
        )
    if isinstance(source_packet, dict):
        context_chunks.extend(str(x) for x in source_packet.get("model_ids") or [])
        context_chunks.extend(str(x) for x in source_packet.get("api_patterns") or [])

    migration_payload: dict[str, str] = {}
    router_urls = list(seeds)
    if profile is not None:
        from conduit.packet.migration_evidence import build_migration_evidence

        evidence = build_migration_evidence(
            context_chunks=context_chunks,
            profile=profile,
            search_queries=queries,
            model_ids=(source_packet or {}).get("model_ids")
            if isinstance(source_packet, dict)
            else None,
            max_pages=8,
            demo_openapi=True,
        )
        migration_payload = evidence.as_prompt_dict()
        router_urls = list(dict.fromkeys(evidence.router_urls + seeds))
        warnings.extend(evidence.warnings)
        if emit is not None:
            emit(
                f"[packet-enrich] prefetched {len(evidence.docs)} doc(s), "
                f"{len(evidence.code_examples)} example(s), "
                f"{len(evidence.openapi_structs)} openapi path(s)"
            )

    research_prefix = (
        "Research phase (required): Read migration_docs, code_examples, and "
        "openapi_structs pre-loaded below. Use fetch_url on seed_urls for any "
        "gap before emitting rules. Do not guess API successors.\n"
    )
    if publisher or not has_source_usage:
        instructions = (
            research_prefix
            + "Use tools (web_search, fetch_url, read_file, grep) to gather "
            "more grounded migration facts from seed_urls / suggested_queries. "
            "This is a publisher catalog packet (no consumer source_packet). "
            "Emit rules covering detect_signals up to to_version. "
            "Do not invent path successors or call shapes. "
            "Emit final JSON with notes, sources, and rules."
        )
    else:
        instructions = (
            research_prefix
            + "Use tools (web_search, fetch_url, read_file, grep) to gather "
            "more grounded migration facts from seed_urls / suggested_queries "
            "and the consumer source_packet. "
            "Only emit rules for source_packet model_ids / usages / api_patterns. "
            "Do not invent path successors or call shapes. "
            "If missed_coverage is non-empty, those rows are the only required adds. "
            "Emit final JSON with notes, sources, and rules."
        )
    user_payload = {
        "package": package,
        "from_version": from_version,
        "to_version": to_version,
        "ecosystem": ecosystem,
        "source_packet": source_packet or {},
        "detect_signals": _signal_summary(scoped_signals, package),
        "existing_rule_count": len(base.get("rules") or []),
        "missed_coverage": missed_items or [],
        "ignore": ignore_payload,
        "seed_urls": router_urls,
        "allow_hosts": hosts or ["github.com"],
        "suggested_queries": queries,
        "instructions": instructions,
    }
    user_payload.update(migration_payload)
    system = (
        _EVIDENCE_SYSTEM
        + " Research first: read pre-loaded migration_docs / examples / openapi_structs, "
        "then fetch_url any missing facts before emitting rules. Final reply must be JSON only."
    )
    executor: RepoToolExecutor | None = None
    if root is not None:
        executor = RepoToolExecutor(
            root=root,
            ignore=ignore,
            allow_writes=False,
            allow_run_tests=False,
        )

    def _exec(name: str, args: dict[str, Any]) -> str:
        if executor is None:
            if name == "fetch_url":
                # Allow fetch even without a consumer root
                tmp = RepoToolExecutor(
                    root=Path("."),
                    allow_writes=False,
                    allow_run_tests=False,
                )
                return tmp._fetch_url(args)
            return json.dumps({"error": f"tool {name!r} requires a consumer repo root"})
        return executor(name, args)

    try:
        from conduit.llm.tools import resolve_max_turns, resolve_reasoning_effort

        run_agent = getattr(client, "run_agent", None)
        label = "LLM coverage retry" if missed_items else "LLM packet enrichment"
        if callable(run_agent):
            max_turns = min(16, resolve_max_turns(32))
            if emit is not None:
                emit(
                    f"{label} for {package} "
                    f"(effort={resolve_reasoning_effort()}, max_turns={max_turns})…"
                )
            data = run_agent(
                system=system,
                user=json.dumps(user_payload),
                tools=agent_tools(mode="enrich"),
                tool_executor=_exec,
                max_turns=max_turns,
            )
        else:
            if emit is not None:
                emit(
                    f"{label} for {package} "
                    f"(effort={resolve_reasoning_effort()}, one-shot)…"
                )
            data = client.complete_json(
                system=system,
                user=json.dumps(user_payload),
            )
    except Exception as exc:
        warnings.append(f"LLM packet enrichment failed: {exc}")
        return base, warnings

    if not data or not isinstance(data, dict):
        warnings.append("LLM packet enrichment returned empty/invalid JSON")
        return base, warnings

    llm_rules = data.get("rules")
    if not isinstance(llm_rules, list):
        warnings.append("LLM packet enrichment missing rules list")
        return base, warnings
    llm_rules = normalize_llm_rules(llm_rules)

    probe = {
        "packet_id": base.get("packet_id"),
        "package": base.get("package", package),
        "ecosystem": base.get("ecosystem", ecosystem),
        "from_version": base.get("from_version", from_version),
        "to_version": base.get("to_version", to_version),
        "sources": list(base.get("sources") or []),
        "notes": base.get("notes"),
        "rules": collapse_dependency_bumps(
            merge_packet_rules(list(base.get("rules") or []), llm_rules),
            package=package,
            from_version=from_version,
            to_version=to_version,
        ),
    }
    if base.get("side_effects"):
        probe["side_effects"] = list(base["side_effects"])
    if data.get("notes"):
        note = str(data["notes"])
        prev = str(probe.get("notes") or "")
        probe["notes"] = f"{prev}\n{note}".strip() if prev else note
    side = data.get("side_effects")
    if isinstance(side, list) and side:
        existing = list(probe.get("side_effects") or [])
        existing.extend(s for s in side if isinstance(s, dict))
        probe["side_effects"] = existing
    for src in data.get("sources") or []:
        if isinstance(src, dict) and src.get("url"):
            probe.setdefault("sources", []).append(
                {
                    "url": str(src["url"]),
                    "kind": normalize_source_kind(src.get("kind") or "docs"),
                }
            )
    for url in seeds:
        probe.setdefault("sources", []).append({"url": url, "kind": "docs"})

    probe["sources"] = normalize_packet_sources(probe.get("sources"))
    if "side_effects" in probe:
        probe["side_effects"] = normalize_packet_side_effects(probe.get("side_effects"))

    errs = validate_packet(probe)
    if errs:
        warnings.append(f"LLM packet failed validation: {errs[:3]}")
        return base, warnings

    warnings.append(
        f"LLM packet enrichment added rules via agent tools "
        f"({len(seeds)} seed URL(s), {len(queries)} quer(ies))"
    )
    return probe, warnings


def load_fixture_openai_packet() -> dict[str, Any]:
    """Offline demo packet derived from classic openai deprecation fixtures."""
    return {
        "packet_id": "openai-0.28.1-1.0.0",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "sources": [
            {
                "url": "https://platform.openai.com/docs/deprecations",
                "kind": "docs",
            }
        ],
        "notes": (
            "Offline fixture packet for demo-consumer (model + param renames).\n\n"
            "Decision rationale:\n"
            "- gpt-4-0613 → gpt-4o from OpenAI deprecations; gpt-4o supports "
            "v1/chat/completions."
        ),
        "rules": [
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
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
                "reason": (
                    "Deprecated model gpt-4-0613; documented replacement gpt-4o "
                    "supports v1/chat/completions. "
                    "Source: https://platform.openai.com/docs/deprecations"
                ),
            },
            {
                "type": "AST_PARAM_RENAME",
                "target_files": ["*.py", "*.ts", "*.js"],
                "function_target": "chat.completions.create",
                "old_param": "max_tokens",
                "new_param": "max_completion_tokens",
                "reason": (
                    "OpenAI chat.completions.create renamed max_tokens to "
                    "max_completion_tokens for newer models."
                ),
            },
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "0.28.1",
                "to_version": "1.0.0",
                "ecosystems": ["pip", "pyproject"],
                "reason": "Major SDK bump required for the modern OpenAI Python client.",
            },
        ],
    }


def ensure_packet(
    root: Path,
    signals: list[ChangeSignal],
    *,
    package: str,
    packet_path: Path | None = None,
    installed: dict[str, str] | None = None,
    use_fixture_fallback: bool = True,
    refresh: bool = False,
    client_state: Any | None = None,
    source_packet: dict[str, Any] | None = None,
    log: Any | None = None,
) -> PacketEnsureResult:
    """Load explicit packet, cache, signal-synth, or openai fixture."""
    from conduit.packet.cache import find_cached_packet, cache_path

    if packet_path and packet_path.is_file():
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        return PacketEnsureResult(
            packet=packet,
            from_source="file",
            to_source="file",
            warnings=[
                "Using published packet file; LLM packet synthesis/enrichment skipped"
            ],
        )

    source = source_packet
    if source is None and client_state is not None and hasattr(client_state, "to_dict"):
        source = client_state.to_dict()

    from_v = _PLACEHOLDER_FROM
    to_v = _PLACEHOLDER_TO
    from_source = "placeholder"
    to_source = "placeholder"
    eco = "pypi"
    pkg_signals = [s for s in signals if s.package.lower() == package.lower()]
    scoped_signals = filter_signals_to_source(pkg_signals, source, package=package)

    src_installed = str((source or {}).get("installed_version") or "").strip()
    if src_installed:
        from_v = src_installed
        from_source = "source"
    else:
        manifest_v = _installed_version(installed, package)
        if manifest_v:
            from_v = manifest_v
            from_source = "manifest"

    bump_signals = [
        s
        for s in (scoped_signals or pkg_signals)
        if s.change_type in {"SDK_MAJOR_BUMP", "DEPENDENCY_BUMP", "SDK_BUMP"}
        and s.to_version
    ]
    chosen = None
    from_parsed = _parse_ver(from_v) if from_source != "placeholder" else None
    for s in bump_signals:
        to_parsed = _parse_ver(s.to_version)
        if from_parsed is not None and to_parsed is not None and to_parsed > from_parsed:
            chosen = s
            break
    if chosen is None and bump_signals:
        chosen = bump_signals[-1]
        to_parsed = _parse_ver(chosen.to_version)
        if from_parsed is not None and to_parsed is not None and to_parsed < from_parsed:
            chosen = None
    if chosen is not None:
        to_v = str(chosen.to_version)
        to_source = "signal"
        eco = chosen.ecosystem or eco
        if from_source == "placeholder" and chosen.from_version:
            from_v = chosen.from_version
            from_source = "signal"

    if to_source == "placeholder":
        # DEPENDENCY_BUMP often lives on suggested_rules without signal.to_version
        rule_to = _to_version_from_rules(
            [r for s in (scoped_signals or pkg_signals) for r in s.suggested_rules],
            package,
        )
        if rule_to:
            to_v = rule_to
            to_source = "rule"

    if not refresh:
        cached = find_cached_packet(root, package, from_v, to_v)
        if cached:
            return PacketEnsureResult(
                packet=cached,
                from_source="cache" if from_source == "placeholder" else from_source,
                to_source="cache" if to_source == "placeholder" else to_source,
            )

    packet = packet_from_signals(
        scoped_signals or pkg_signals,
        package=package,
        ecosystem=eco,
        from_version=from_v,
        to_version=to_v,
        companion_signals=signals,
    )
    used_fixture = False
    profile = None
    try:
        from conduit.detect.vendor_profile import profile_for_package

        profile = profile_for_package(package)
    except Exception:
        profile = None
    if (
        not packet.get("rules")
        and use_fixture_fallback
        and profile is not None
        and profile.demo_packet_fallback
    ):
        packet = load_fixture_openai_packet()
        used_fixture = True
        if from_source == "placeholder":
            from_v = str(packet.get("from_version") or from_v)
            from_source = "fixture"
        if to_source == "placeholder":
            to_v = str(packet.get("to_version") or to_v)
            to_source = "fixture"

    if to_source == "placeholder":
        rule_to = _to_version_from_rules(list(packet.get("rules") or []), package)
        if rule_to:
            to_v = rule_to
            to_source = "rule"

    _apply_versions(packet, package=package, from_version=from_v, to_version=to_v)

    warnings: list[str] = []
    if refresh:
        warnings.append(
            f"refreshed packet cache for {package} {from_v} -> {to_v} "
            "(ignored existing .conduit/packets entry)"
        )
    if from_source == "placeholder":
        warnings.append(
            f"from_version defaulted to {from_v!r} "
            f"(no manifest or detect signal version for {package})"
        )
    if to_source == "placeholder":
        warnings.append(
            f"to_version defaulted to {to_v!r} "
            f"(no detect signal or DEPENDENCY_BUMP target for {package})"
        )
    if used_fixture:
        warnings.append(
            f"using offline {package} fixture packet because signal synthesis produced no rules"
        )

    # Evidence + LLM enrichment (live only; demo keeps fixtures / signal rules)
    if not use_fixture_fallback:
        packet, enrich_warnings = synthesize_from_evidence(
            package=package,
            from_version=str(packet.get("from_version") or from_v),
            to_version=str(packet.get("to_version") or to_v),
            ecosystem=str(packet.get("ecosystem") or eco),
            signals=scoped_signals or pkg_signals,
            base=packet,
            root=root,
            source_packet=source,
            log=log,
        )
        warnings.extend(enrich_warnings)
        _apply_versions(
            packet,
            package=package,
            from_version=str(packet.get("from_version") or from_v),
            to_version=str(packet.get("to_version") or to_v),
        )
        packet["rules"] = collapse_dependency_bumps(
            fold_companion_rules(
                list(packet.get("rules") or []),
                signals,
                package=package,
            ),
            package=package,
            from_version=str(packet.get("from_version") or from_v),
            to_version=str(packet.get("to_version") or to_v),
        )
        if client_state is not None:
            from conduit.detect.coverage import build_coverage_report

            report = build_coverage_report(
                package=package,
                state=client_state,
                signals=scoped_signals or pkg_signals,
                packet=packet,
            )
            if report.missed:
                missed = [
                    {"kind": i.kind, "value": i.value, "detail": i.detail}
                    for i in report.missed
                ]
                packet, retry_warnings = synthesize_from_evidence(
                    package=package,
                    from_version=str(packet.get("from_version") or from_v),
                    to_version=str(packet.get("to_version") or to_v),
                    ecosystem=str(packet.get("ecosystem") or eco),
                    signals=scoped_signals or pkg_signals,
                    base=packet,
                    root=root,
                    source_packet=source,
                    missed_items=missed,
                    log=log,
                )
                warnings.extend(retry_warnings)
                warnings.append(
                    f"coverage retry for {len(missed)} missed client item(s)"
                )
                _apply_versions(
                    packet,
                    package=package,
                    from_version=str(packet.get("from_version") or from_v),
                    to_version=str(packet.get("to_version") or to_v),
                )
                packet["rules"] = collapse_dependency_bumps(
                    fold_companion_rules(
                        list(packet.get("rules") or []),
                        signals,
                        package=package,
                    ),
                    package=package,
                    from_version=str(packet.get("from_version") or from_v),
                    to_version=str(packet.get("to_version") or to_v),
                )

    save_packet(cache_path(root, package, packet["from_version"], packet["to_version"]), packet)
    return PacketEnsureResult(
        packet=packet,
        from_source=from_source,
        to_source=to_source,
        used_fixture=used_fixture,
        warnings=warnings,
    )
