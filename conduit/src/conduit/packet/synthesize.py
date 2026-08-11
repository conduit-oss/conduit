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


@dataclass
class PacketEnsureResult:
    packet: dict[str, Any]
    from_source: str = "placeholder"  # file | cache | signal | manifest | rule | fixture | placeholder
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
) -> dict[str, Any]:
    """Assemble a packet from ChangeSignal suggested_rules (deterministic)."""
    pkg_signals = [s for s in signals if s.package.lower() == package.lower()]
    if pkg_signals:
        # Prefer lockfile jump versions when present
        for s in pkg_signals:
            if s.from_version and s.to_version:
                from_version = s.from_version
                to_version = s.to_version
                if s.ecosystem:
                    ecosystem = s.ecosystem
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
    packet["rules"] = rules
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
            "packet_id, package, ecosystem, from_version, to_version, sources, notes, rules. "
            "Rules may use EXACT_STRING_REPLACE, REGEX_REPLACE, AST_PARAM_RENAME, "
            "DEPENDENCY_BUMP, AST_IMPORT_REWRITE, AST_ATTR_RENAME, AST_CALL_REWRITE. "
            "Only propose replacements grounded in the provided changelog/docs. "
            "If a successor is unknown, put it in notes — do not invent paths or callees. "
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
            system="You author Conduit migration packets. JSON only. Never invent API successors.",
            user=json.dumps(prompt),
        )
        if data and not validate_packet(data):
            return data
        if data:
            packet["rules"] = data.get("rules") or packet.get("rules") or []
            packet["notes"] = data.get("notes") or packet.get("notes")
    except Exception:
        pass
    return packet


_EVIDENCE_SYSTEM = (
    "You are a Staff Software Engineer authoring Conduit Migration Packets. "
    "Emit JSON only with keys: notes (string), sources (list of {url, kind}), rules (list). "
    "Allowed rule types: EXACT_STRING_REPLACE, REGEX_REPLACE, AST_PARAM_RENAME, "
    "DEPENDENCY_BUMP, AST_IMPORT_REWRITE, AST_ATTR_RENAME, AST_CALL_REWRITE. "
    "Every path replace, param rename, and call rewrite MUST be supported by the evidence "
    "excerpts (cite URLs in notes). "
    "For AST_PARAM_RENAME include explicit function_target(s) taken from evidence — "
    "do not assume ChatCompletion vs chat.completions. "
    "If a removed endpoint/param has no stated successor, mention it in notes and do NOT "
    "invent replace/new_callee/new_param. "
    "Do not invent model ids. "
    "When proposing a model EXACT_STRING_REPLACE, the replacement must support the client "
    "endpoints implied by detect_signals / evidence (see model Supported endpoints tables). "
    "Every rule MUST include a short 'reason' string explaining why it was chosen "
    "(cite the source URL). "
    "Honor any ignore list: do not emit rules whose only effect would be rewriting "
    "ignored contract patterns/files (LEGACY_/FORBIDDEN_ oracles)."
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
) -> tuple[dict[str, Any], list[str]]:
    """
    LLM-author rules via Responses agent tools (web_search / fetch_url / read_file).
    Returns (packet, warnings).
    """
    from conduit.llm import get_llm_client
    from conduit.llm.executors import RepoToolExecutor
    from conduit.llm.tools import agent_tools
    from conduit.repair_ignore import IgnoreList, build_ignore_list

    warnings: list[str] = []
    client = get_llm_client()
    if client is None:
        warnings.append("LLM packet enrichment skipped (no LLM configured)")
        return base, warnings

    seeds, hosts, queries = _module_evidence_meta(package, from_version, to_version)
    if not seeds and not queries:
        warnings.append(
            f"LLM packet enrichment skipped (no evidence seeds for package {package!r})"
        )
        return base, warnings

    ignore = IgnoreList()
    ignore_payload: dict[str, Any] = {}
    if root is not None:
        ignore = build_ignore_list(root, base)
        ignore_payload = ignore.to_prompt_dict()

    user_payload = {
        "package": package,
        "from_version": from_version,
        "to_version": to_version,
        "ecosystem": ecosystem,
        "detect_signals": _signal_summary(signals, package),
        "existing_rule_count": len(base.get("rules") or []),
        "ignore": ignore_payload,
        "seed_urls": seeds,
        "allow_hosts": hosts
        or ["platform.openai.com", "developers.openai.com", "github.com"],
        "suggested_queries": queries,
        "instructions": (
            "Use tools (web_search, fetch_url, read_file, code_interpreter) to gather "
            "grounded migration facts from seed_urls / suggested_queries. "
            "Do not invent path successors or call shapes. "
            "Emit final JSON with notes, sources, and rules."
        ),
    }
    system = (
        _EVIDENCE_SYSTEM
        + " Use tools as needed before answering. Final reply must be JSON only."
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
        from conduit.llm.tools import resolve_max_turns

        run_agent = getattr(client, "run_agent", None)
        if callable(run_agent):
            data = run_agent(
                system=system,
                user=json.dumps(user_payload),
                tools=agent_tools(mode="enrich"),
                tool_executor=_exec,
                max_turns=min(16, resolve_max_turns(32)),
            )
        else:
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

    probe = {
        "packet_id": base.get("packet_id"),
        "package": base.get("package", package),
        "ecosystem": base.get("ecosystem", ecosystem),
        "from_version": base.get("from_version", from_version),
        "to_version": base.get("to_version", to_version),
        "sources": list(base.get("sources") or []),
        "notes": base.get("notes"),
        "rules": merge_packet_rules(list(base.get("rules") or []), llm_rules),
    }
    if data.get("notes"):
        note = str(data["notes"])
        prev = str(probe.get("notes") or "")
        probe["notes"] = f"{prev}\n{note}".strip() if prev else note
    for src in data.get("sources") or []:
        if isinstance(src, dict) and src.get("url"):
            probe.setdefault("sources", []).append(
                {"url": str(src["url"]), "kind": str(src.get("kind") or "docs")}
            )
    for url in seeds:
        probe.setdefault("sources", []).append({"url": url, "kind": "docs"})

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
) -> PacketEnsureResult:
    """Load explicit packet, cache, signal-synth, or openai fixture."""
    from conduit.packet.cache import find_cached_packet, cache_path

    if packet_path and packet_path.is_file():
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        return PacketEnsureResult(
            packet=packet,
            from_source="file",
            to_source="file",
        )

    from_v = _PLACEHOLDER_FROM
    to_v = _PLACEHOLDER_TO
    from_source = "placeholder"
    to_source = "placeholder"
    eco = "pypi"
    pkg_signals = [s for s in signals if s.package.lower() == package.lower()]

    for s in pkg_signals:
        if s.from_version and s.to_version:
            from_v, to_v = s.from_version, s.to_version
            from_source = to_source = "signal"
            eco = s.ecosystem or eco
            break

    if from_source == "placeholder":
        manifest_v = _installed_version(installed, package)
        if manifest_v:
            from_v = manifest_v
            from_source = "manifest"

    if to_source == "placeholder":
        for s in pkg_signals:
            if s.to_version:
                to_v = s.to_version
                to_source = "signal"
                eco = s.ecosystem or eco
                break

    if to_source == "placeholder":
        # DEPENDENCY_BUMP often lives on suggested_rules without signal.to_version
        rule_to = _to_version_from_rules(
            [r for s in pkg_signals for r in s.suggested_rules],
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
        signals, package=package, ecosystem=eco, from_version=from_v, to_version=to_v
    )
    used_fixture = False
    if not packet.get("rules") and use_fixture_fallback and package.lower() == "openai":
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
            "using offline openai fixture packet because signal synthesis produced no rules"
        )

    # Evidence + LLM enrichment (live only; demo keeps fixtures / signal rules)
    if not use_fixture_fallback:
        packet, enrich_warnings = synthesize_from_evidence(
            package=package,
            from_version=str(packet.get("from_version") or from_v),
            to_version=str(packet.get("to_version") or to_v),
            ecosystem=str(packet.get("ecosystem") or eco),
            signals=signals,
            base=packet,
            root=root,
        )
        warnings.extend(enrich_warnings)
        _apply_versions(
            packet,
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
