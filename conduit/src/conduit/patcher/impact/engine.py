"""Orchestrate pre-apply impact analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from conduit.patcher.impact.llm import llm_impact_review
from conduit.patcher.impact.mechanical import mechanical_impact_pass
from conduit.patcher.impact.store import load_learned_impact_rules, save_impact_report
from conduit.patcher.impact.validator import validate_impact_post_rules
from conduit.patcher.impact.vendor import default_banned_kwargs_on
from conduit.patcher.post_rules.store import merge_post_rules
from conduit.patcher import apply_packet
from conduit.test_gen import oracle_scan_rels

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class ImpactReport:
    findings: list[dict[str, Any]] = field(default_factory=list)
    post_rules: list[dict[str, Any]] = field(default_factory=list)
    defer_paths: set[str] = field(default_factory=set)
    packet_patches: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    block_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "findings": self.findings,
            "post_rules": self.post_rules,
            "defer_paths": sorted(self.defer_paths),
            "packet_patches": self.packet_patches,
        }


def _file_windows(root: Path, paths: set[str], *, limit: int = 8) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rel in sorted(paths)[:limit]:
        path = root / rel.replace("/", "\\")
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append({"path": rel.replace("\\", "/"), "text": text[:8000]})
    return out


def analyze_impacts(
    root: Path,
    packet: dict[str, Any],
    *,
    file_allowlist: list[Path] | None = None,
    log: LogFn | None = None,
    use_llm: bool = True,
) -> ImpactReport:
    """Dry-run apply plan + mechanical + LLM impact review."""
    emit = log or _noop
    root = root.resolve()
    report = ImpactReport()

    dry = apply_packet(root, packet, dry_run=True, file_allowlist=file_allowlist)
    planned = {p.replace("\\", "/") for p in dry.files_modified}

    oracle_rels = set(
        oracle_scan_rels(root, packet, file_allowlist=file_allowlist)
    )
    allowlist_rels = set()
    if file_allowlist:
        for p in file_allowlist:
            try:
                allowlist_rels.add(p.relative_to(root).as_posix())
            except ValueError:
                allowlist_rels.add(p.name)

    mech_findings, mech_rules, mech_defer = mechanical_impact_pass(
        root,
        packet,
        planned_paths=planned,
        oracle_paths=oracle_rels,
        allowlist_paths=allowlist_rels,
    )
    report.findings.extend(mech_findings)
    report.post_rules.extend(mech_rules)
    report.defer_paths.update(mech_defer)

    learned = load_learned_impact_rules(root)
    if learned:
        report.post_rules = merge_post_rules(report.post_rules, learned)

    vendor_kwargs = default_banned_kwargs_on(packet)
    if vendor_kwargs:
        report.packet_patches["anticheat"] = {"banned_kwargs_on": vendor_kwargs}

    scan_paths = planned | report.defer_paths | set(
        f.get("path") for f in mech_findings if f.get("path")
    )
    if use_llm:
        emit("[impact] LLM additive review…")
        llm_findings, llm_rules, _llm_defer = llm_impact_review(
            packet=packet,
            mechanical_findings=mech_findings,
            planned_paths=sorted(planned),
            file_windows=_file_windows(root, scan_paths),
            log=emit,
        )
        report.findings.extend(llm_findings)
        validated = []
        for rule in llm_rules:
            errs = validate_impact_post_rules([rule], packet=packet)
            if not errs:
                validated.append(rule)
        report.post_rules = merge_post_rules(report.post_rules, validated)
        # LLM defer_paths are ignored: always attempt packet apply first.
        # Mechanical defer_during_apply (with a concrete post_rule) still applies.
    for finding in report.findings:
        if finding.get("action") == "block" and finding.get("required"):
            report.blocked = True
            report.block_reason = str(finding.get("detail") or finding.get("kind"))
            break

    required_errors = [
        f
        for f in report.findings
        if f.get("required") and f.get("severity") == "error" and f.get("action") == "fix"
    ]
    if required_errors and not report.post_rules and not report.defer_paths:
        report.blocked = True
        report.block_reason = (
            "Required impact fixes have no post_rules or defer policy: "
            + str(required_errors[0].get("detail"))
        )

    if report.post_rules:
        emit(f"[impact] queued {len(report.post_rules)} post-rule(s)")
    if report.defer_paths:
        emit(f"[impact] defer generic apply on {len(report.defer_paths)} path(s)")
    if report.blocked:
        emit(f"[impact] blocked: {report.block_reason}")

    try:
        save_impact_report(root, report.to_dict())
    except OSError:
        pass

    return report


def merge_runtime_packet(packet: dict[str, Any], patches: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge in-memory packet patches (never writes consumer packet file)."""
    if not patches:
        return packet
    merged = dict(packet)
    for key, val in patches.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            inner = dict(merged[key])
            inner.update(val)
            merged[key] = inner
        else:
            merged[key] = val
    return merged
