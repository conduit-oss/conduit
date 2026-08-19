"""Source packet (client baseline) vs migration signals / packet coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.path_callees import (
    normalize_api_path,
    path_for_api_pattern,
)


# Client usage vs packet: does this hop change it, leave it, or lack a rule?
STATUS_WILL_MIGRATE = "will_migrate"
STATUS_KEEP = "keep"
STATUS_NO_RULE = "no_rule"
STATUS_UNMAPPED = "unmapped"

_STATUS_LABEL = {
    STATUS_WILL_MIGRATE: "WILL MIGRATE",
    STATUS_KEEP: "KEEP",
    STATUS_NO_RULE: "NO RULE",
    STATUS_UNMAPPED: "UNMAPPED",
}
_STATUS_BLURB = {
    STATUS_WILL_MIGRATE: "packet has a rule to change this",
    STATUS_KEEP: "already a replacement target; no change needed",
    STATUS_NO_RULE: "used in this repo; packet has no migrate-from rule",
    STATUS_UNMAPPED: "not a known /v1 route or SDK callee; not a migration gap",
}
_STATUS_ORDER = (
    STATUS_WILL_MIGRATE,
    STATUS_KEEP,
    STATUS_NO_RULE,
    STATUS_UNMAPPED,
)


@dataclass
class CoverageItem:
    kind: str  # model | api_pattern | callee
    value: str
    status: str  # will_migrate | keep | no_rule | unmapped
    detail: str = ""


@dataclass
class PacketCoverageReport:
    package: str
    source_packet: dict[str, Any]
    migration_summary: dict[str, Any]
    items: list[CoverageItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def will_migrate(self) -> list[CoverageItem]:
        return [i for i in self.items if i.status == STATUS_WILL_MIGRATE]

    @property
    def keep(self) -> list[CoverageItem]:
        return [i for i in self.items if i.status == STATUS_KEEP]

    @property
    def no_rule(self) -> list[CoverageItem]:
        return [i for i in self.items if i.status == STATUS_NO_RULE]

    @property
    def unmapped(self) -> list[CoverageItem]:
        return [i for i in self.items if i.status == STATUS_UNMAPPED]

    # Aliases used by run summary / older tests
    @property
    def missed(self) -> list[CoverageItem]:
        return self.no_rule

    @property
    def caught(self) -> list[CoverageItem]:
        return self.will_migrate

    @property
    def ok(self) -> list[CoverageItem]:
        return self.keep

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "source_packet": self.source_packet,
            "migration_summary": self.migration_summary,
            "coverage": [
                {
                    "kind": i.kind,
                    "value": i.value,
                    "status": i.status,
                    "detail": i.detail,
                }
                for i in self.items
            ],
            "notes": list(self.notes),
        }


def build_source_packet(
    state: PackageClientState | None,
    *,
    package: str,
) -> dict[str, Any]:
    """Serialize the client baseline as a conduit source packet."""
    if state is None:
        return {
            "packet_kind": "conduit_source_packet",
            "package": package,
            "installed_version": None,
            "model_ids": [],
            "api_patterns": [],
            "import_files": [],
            "ecosystems": [],
            "source": "missing",
            "notes": ["No client baseline scanned for this package."],
        }
    data = state.to_dict()
    data["packet_kind"] = "conduit_source_packet"
    return data


def _signal_touches_model(signal: ChangeSignal, model_id: str) -> bool:
    mid = model_id.lower()
    if (signal.affected_pattern or "").lower() == mid:
        return True
    for rule in signal.suggested_rules:
        if rule.get("type") != "EXACT_STRING_REPLACE":
            continue
        if str(rule.get("match") or "").lower() == mid:
            return True
    return False


def _signal_touches_path(signal: ChangeSignal, path: str) -> bool:
    norm = normalize_api_path(path)
    if not norm:
        return False
    for candidate in (
        signal.affected_pattern,
        signal.replacement_pattern,
        (signal.hints or {}).get("path"),
        (signal.hints or {}).get("old_path"),
        (signal.hints or {}).get("new_path"),
    ):
        if normalize_api_path(str(candidate) if candidate else None) == norm:
            return True
    desc = (signal.description or "").lower()
    return norm.lower() in desc


def _successor_from_rules(
    rule_hits: list[dict[str, Any]],
    hits: list[ChangeSignal],
) -> str:
    for rule in rule_hits:
        for key in ("replace", "new_callee", "new_param", "new_attr", "new_import"):
            val = str(rule.get(key) or "").strip()
            if val:
                return val
    for signal in hits:
        if signal.replacement_pattern:
            return str(signal.replacement_pattern)
    return ""


def _rule_touches_model(rule: dict[str, Any], model_id: str) -> bool:
    mid = model_id.lower()
    if rule.get("type") == "EXACT_STRING_REPLACE":
        return str(rule.get("match") or "").lower() == mid
    return False


def _rule_touches_path(rule: dict[str, Any], path: str) -> bool:
    norm = normalize_api_path(path)
    if not norm:
        return False
    if rule.get("type") == "EXACT_STRING_REPLACE":
        for key in ("match",):
            if normalize_api_path(str(rule.get(key) or "")) == norm:
                return True
    return False


def _rule_touches_callee(rule: dict[str, Any], callee: str) -> bool:
    want = (callee or "").strip().lower()
    if not want:
        return False
    rtype = str(rule.get("type") or "")
    if rtype == "AST_CALL_REWRITE":
        return want == str(rule.get("old_callee") or "").strip().lower()
    if rtype == "AST_ATTR_RENAME":
        return want == str(rule.get("old_attr") or "").strip().lower()
    if rtype == "EXACT_STRING_REPLACE":
        return want == str(rule.get("match") or "").strip().lower()
    return False


def _signal_touches_callee(signal: ChangeSignal, callee: str) -> bool:
    want = (callee or "").strip().lower()
    if not want:
        return False
    for candidate in (signal.affected_pattern, signal.replacement_pattern):
        if str(candidate or "").strip().lower() == want:
            return True
    return any(_rule_touches_callee(r, callee) for r in signal.suggested_rules)


def _migration_summary(
    signals: list[ChangeSignal],
    packet: dict[str, Any] | None,
    *,
    package: str,
) -> dict[str, Any]:
    pkg_signals = [s for s in signals if s.package.lower() == package.lower()]
    by_type: dict[str, int] = {}
    for s in pkg_signals:
        by_type[s.change_type] = by_type.get(s.change_type, 0) + 1
    rules = list((packet or {}).get("rules") or [])
    return {
        "packet_kind": "conduit_migration_packet",
        "packet_id": (packet or {}).get("packet_id"),
        "package": package,
        "from_version": (packet or {}).get("from_version"),
        "to_version": (packet or {}).get("to_version"),
        "signal_count": len(pkg_signals),
        "signals_by_type": by_type,
        "rule_count": len(rules),
        "rule_types": sorted({str(r.get("type")) for r in rules if isinstance(r, dict)}),
        "notes": (packet or {}).get("notes"),
        "sources": (packet or {}).get("sources") or [],
    }


def build_coverage_report(
    *,
    package: str,
    state: PackageClientState | None,
    signals: list[ChangeSignal],
    packet: dict[str, Any] | None = None,
) -> PacketCoverageReport:
    """Diff client source packet against detect signals + migration packet rules."""
    source = build_source_packet(state, package=package)
    summary = _migration_summary(signals, packet, package=package)
    pkg_signals = [s for s in signals if s.package.lower() == package.lower()]
    rules = [r for r in (packet or {}).get("rules") or [] if isinstance(r, dict)]
    items: list[CoverageItem] = []
    notes: list[str] = []

    if (
        not source.get("model_ids")
        and not source.get("api_patterns")
        and not source.get("usages")
    ):
        notes.append(
            "Source packet empty (no model_ids / api_patterns). "
            "Coverage cannot flag misses — baseline unknown."
        )

    successor_models = {
        (s.replacement_pattern or "").lower()
        for s in pkg_signals
        if s.change_type in {"MODEL_DEPRECATION", "MODEL_REMOVED"} and s.replacement_pattern
    }
    for rule in rules:
        if str(rule.get("type") or "") != "EXACT_STRING_REPLACE":
            continue
        repl = str(rule.get("replace") or "").strip().lower()
        if repl:
            successor_models.add(repl)

    usage_ids = {
        str(u.get("id") or "").strip().lower()
        for u in (source.get("usages") or [])
        if isinstance(u, dict) and str(u.get("id") or "").strip()
    }
    known_models_lower: set[str] = set()
    if usage_ids:
        try:
            from conduit.detect.vendor_profile import collect_known_ids

            known_models_lower = {
                m.lower() for m in collect_known_ids(package, demo=False)
            }
        except Exception:  # noqa: BLE001 — fail soft; score all model_ids
            known_models_lower = set()

    for model_id in source.get("model_ids") or []:
        mid = str(model_id).strip()
        mid_lower = mid.lower()
        # Usage ids are often helper/function names, not model strings. When the
        # known catalog is available, skip usage-id tokens that are not in it.
        if (
            mid_lower in usage_ids
            and known_models_lower
            and mid_lower not in known_models_lower
        ):
            continue
        hits = [s for s in pkg_signals if _signal_touches_model(s, model_id)]
        rule_hits = [r for r in rules if _rule_touches_model(r, model_id)]
        if hits or rule_hits:
            successor = _successor_from_rules(rule_hits, hits)
            if successor:
                detail = f"will replace with {successor}"
            else:
                detail = f"{len(rule_hits)} packet rule(s)" if rule_hits else (
                    f"{hits[0].change_type}:{hits[0].affected_pattern}"
                    if hits else ""
                )
            items.append(
                CoverageItem(
                    kind="model",
                    value=str(model_id),
                    status=STATUS_WILL_MIGRATE,
                    detail=detail,
                )
            )
        elif str(model_id).lower() in successor_models:
            items.append(
                CoverageItem(
                    kind="model",
                    value=str(model_id),
                    status=STATUS_KEEP,
                    detail="already a replacement target; leave as-is",
                )
            )
        else:
            items.append(
                CoverageItem(
                    kind="model",
                    value=str(model_id),
                    status=STATUS_NO_RULE,
                    detail="used here; packet has no replace-from rule (may still be current)",
                )
            )

    for pattern in source.get("api_patterns") or []:
        path = path_for_api_pattern(str(pattern)) or (
            normalize_api_path(str(pattern)) if str(pattern).startswith("/v1/") else None
        )
        if path:
            hits = [s for s in pkg_signals if _signal_touches_path(s, path)]
            rule_hits = [r for r in rules if _rule_touches_path(r, path)]
            # Param renames on this path also count
            param_hits = [
                s
                for s in pkg_signals
                if s.change_type == "PARAM_RENAME"
                and (
                    normalize_api_path(str((s.hints or {}).get("path") or "")) == path
                    or normalize_api_path(str((s.hints or {}).get("new_path") or ""))
                    == path
                    or path.lower() in (s.description or "").lower()
                )
            ]
            callee_hits = [
                s for s in pkg_signals if _signal_touches_callee(s, str(pattern))
            ]
            callee_rules = [r for r in rules if _rule_touches_callee(r, str(pattern))]
            if hits or rule_hits or param_hits or callee_hits or callee_rules:
                successor = _successor_from_rules(rule_hits + callee_rules, hits)
                extra = f" → {successor}" if successor else ""
                items.append(
                    CoverageItem(
                        kind="api_pattern",
                        value=str(pattern),
                        status=STATUS_WILL_MIGRATE,
                        detail=f"path {path}{extra}",
                    )
                )
            else:
                items.append(
                    CoverageItem(
                        kind="api_pattern",
                        value=str(pattern),
                        status=STATUS_NO_RULE,
                        detail=f"maps to {path}; packet has no path/param/callee rewrite",
                    )
                )
        else:
            callee_hits = [s for s in pkg_signals if _signal_touches_callee(s, str(pattern))]
            callee_rules = [r for r in rules if _rule_touches_callee(r, str(pattern))]
            if callee_hits or callee_rules:
                successor = _successor_from_rules(callee_rules, callee_hits)
                extra = f" → {successor}" if successor else ""
                items.append(
                    CoverageItem(
                        kind="api_pattern",
                        value=str(pattern),
                        status=STATUS_WILL_MIGRATE,
                        detail=f"callee rewrite{extra}",
                    )
                )
            else:
                items.append(
                    CoverageItem(
                        kind="api_pattern",
                        value=str(pattern),
                        status=STATUS_UNMAPPED,
                        detail="helper/token, not a known /v1 route — not scored as a gap",
                    )
                )

    for usage in source.get("usages") or []:
        if not isinstance(usage, dict):
            continue
        for callee in usage.get("callees") or []:
            if any(i.kind == "api_pattern" and i.value == callee for i in items):
                continue
            hits = [s for s in pkg_signals if _signal_touches_callee(s, str(callee))]
            rule_hits = [r for r in rules if _rule_touches_callee(r, str(callee))]
            successor = _successor_from_rules(rule_hits, hits)
            extra = f" → {successor}" if successor else ""
            items.append(
                CoverageItem(
                    kind="callee",
                    value=str(callee),
                    status=STATUS_WILL_MIGRATE if (hits or rule_hits) else STATUS_NO_RULE,
                    detail=(
                        f"from usage {usage.get('id')}{extra}"
                        if hits or rule_hits
                        else f"from usage {usage.get('id')}; packet has no callee rewrite"
                    ),
                )
            )

    # Vendor signals that do not touch any scanned client usage
    client_models = {str(m).lower() for m in (source.get("model_ids") or [])}
    client_paths = set()
    for pattern in source.get("api_patterns") or []:
        p = path_for_api_pattern(str(pattern)) or normalize_api_path(str(pattern))
        if p:
            client_paths.add(p)

    orphan = 0
    for s in pkg_signals:
        if s.change_type in {"MODEL_DEPRECATION", "MODEL_REMOVED"}:
            aff = (s.affected_pattern or "").lower()
            if aff and aff not in client_models and client_models:
                orphan += 1
        if s.change_type == "API_BREAKING":
            looks = normalize_api_path(s.affected_pattern)
            if looks and client_paths and looks not in client_paths:
                orphan += 1
    if orphan:
        notes.append(
            f"{orphan} vendor signal(s) do not match scanned client usage "
            "(may be fine — not everything in OpenAI docs is used)."
        )

    return PacketCoverageReport(
        package=package,
        source_packet=source,
        migration_summary=summary,
        items=items,
        notes=notes,
    )


def format_coverage_report(report: PacketCoverageReport, *, verbose: bool = False) -> str:
    """Human-readable multi-section report for CLI."""
    lines: list[str] = []
    src = report.source_packet
    mig = report.migration_summary

    lines.append("=== Conduit source packet (client baseline) ===")
    lines.append(f"package: {src.get('package')}  source: {src.get('source')}")
    lines.append(f"installed_version: {src.get('installed_version')!r}")
    lines.append(f"model_ids ({len(src.get('model_ids') or [])}): {src.get('model_ids') or []}")
    lines.append(
        f"api_patterns ({len(src.get('api_patterns') or [])}): "
        f"{src.get('api_patterns') or []}"
    )
    usages = src.get("usages") or []
    if usages:
        lines.append(f"usages ({len(usages)}): {usages[:6]}{' …' if len(usages) > 6 else ''}")
    files = src.get("import_files") or []
    if verbose:
        lines.append(f"import_files ({len(files)}):")
        for f in files:
            lines.append(f"  - {f}")
    else:
        preview = files[:8]
        more = f" … +{len(files) - 8} more" if len(files) > 8 else ""
        lines.append(f"import_files ({len(files)}): {preview}{more}")
    if src.get("notes"):
        for n in src["notes"]:
            lines.append(f"note: {n}")

    lines.append("")
    lines.append("=== Conduit migration packet (after OpenAI sources) ===")
    lines.append(
        f"packet_id: {mig.get('packet_id')!r}  "
        f"{mig.get('from_version')} → {mig.get('to_version')}"
    )
    lines.append(
        f"signals: {mig.get('signal_count')}  "
        f"by_type: {mig.get('signals_by_type') or {}}"
    )
    lines.append(
        f"rules: {mig.get('rule_count')}  types: {mig.get('rule_types') or []}"
    )
    if verbose and mig.get("sources"):
        lines.append("sources:")
        for s in mig["sources"][:12]:
            if isinstance(s, dict):
                lines.append(f"  - {s.get('kind')}: {s.get('url')}")

    lines.append("")
    lines.append("=== Coverage (what this repo uses vs packet rules) ===")
    lines.append("WILL MIGRATE  packet will change this")
    lines.append("KEEP          already the new id / no change needed")
    lines.append("NO RULE       used here; packet has no migrate-from rule")
    lines.append("UNMAPPED      helper/short path; not scored as a gap")
    if not report.items:
        lines.append("(no client model_ids / api_patterns to score)")
    by_status: dict[str, list[CoverageItem]] = {key: [] for key in _STATUS_ORDER}
    for item in report.items:
        by_status.setdefault(item.status, []).append(item)
    for status in _STATUS_ORDER:
        group = by_status.get(status) or []
        if not group:
            continue
        label = _STATUS_LABEL.get(status, status.upper())
        blurb = _STATUS_BLURB.get(status, "")
        lines.append(f"[{label}] {len(group)}  — {blurb}")
        for item in group:
            line = f"  {item.kind} {item.value}"
            if item.detail:
                line += f"  ({item.detail})"
            lines.append(line)
    lines.append(
        "summary: "
        f"will_migrate={len(report.will_migrate)} "
        f"keep={len(report.keep)} "
        f"no_rule={len(report.no_rule)} "
        f"unmapped={len(report.unmapped)} "
        f"total={len(report.items)}"
    )
    for n in report.notes:
        lines.append(f"note: {n}")

    if verbose:
        lines.append("")
        lines.append("=== Source packet JSON ===")
        lines.append(json.dumps(report.source_packet, indent=2))
        lines.append("")
        lines.append("=== Migration summary JSON ===")
        lines.append(json.dumps(report.migration_summary, indent=2))

    return "\n".join(lines)


def save_source_packet(root: Path, source_packet: dict[str, Any]) -> Path:
    """Persist source packet under .conduit/source-packets/ for inspection."""
    pkg = str(source_packet.get("package") or "package")
    out_dir = root / ".conduit" / "source-packets"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{pkg}.json"
    path.write_text(json.dumps(source_packet, indent=2) + "\n", encoding="utf-8")
    return path


def load_source_packet(root: Path, package: str) -> dict[str, Any] | None:
    path = root / ".conduit" / "source-packets" / f"{package}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
