"""End-of-run summary: what changed and what to double-check (core + package)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from conduit.detect.coverage import PacketCoverageReport
from conduit.detect.modules.discovery import load_modules
from conduit.patcher.dependency_update import DEP_RULE_TYPES, dependency_packages
from conduit.patcher.engine import ChangeRecord, PatchReport
from conduit.test_runner import TestResult

_CHANGE_CAP = 20
_REVIEW_CAP = 15


@dataclass
class RunSummary:
    package: str
    packet_id: str
    from_version: str
    to_version: str
    files_modified: list[str] = field(default_factory=list)
    change_lines: list[str] = field(default_factory=list)
    tests: str = ""
    pr_message: str | None = None
    core_review: list[str] = field(default_factory=list)
    package_review: list[str] = field(default_factory=list)
    audit_score: dict[str, Any] | None = None
    audit_log_path: str | None = None

    @property
    def has_review(self) -> bool:
        return bool(self.core_review or self.package_review)


def _change_line(change: ChangeRecord) -> str:
    return f"[{change.rule_type}] {change.path}: {change.detail}"


def _capped(items: Iterable[str], cap: int) -> list[str]:
    out = list(items)
    extra = len(out) - cap
    if extra > 0:
        return out[:cap] + [f"... +{extra} more"]
    return out


def _core_review(
    *,
    packet: dict[str, Any],
    report: PatchReport,
    coverage: PacketCoverageReport | None,
    generated: list[str],
    corrected: list[str],
    test_result: TestResult,
    skip_tests: bool,
    pr_created: bool | None,
    pr_message: str | None,
    detected_signals: list[Any] | None = None,
) -> list[str]:
    items: list[str] = []
    if coverage:
        for item in coverage.no_rule[:_REVIEW_CAP]:
            detail = f" — {item.detail}" if item.detail else ""
            items.append(f"No packet rule for {item.kind} `{item.value}`{detail}")
        leftover = len(coverage.no_rule) - _REVIEW_CAP
        if leftover > 0:
            items.append(f"... +{leftover} more items with no packet rule")
        unmapped_n = len(coverage.unmapped)
        if unmapped_n:
            items.append(
                f"{unmapped_n} client token(s) unmapped (wrappers/short paths; not gaps)"
            )
    for effect in packet.get("side_effects") or []:
        if not isinstance(effect, dict):
            continue
        detail = str(effect.get("detail") or "").strip()
        if not detail:
            continue
        kind = str(effect.get("kind") or "other").strip() or "other"
        items.append(f"Side effect ({kind}): {detail}")
    if any(
        isinstance(rule, dict) and str(rule.get("type") or "") in DEP_RULE_TYPES
        for rule in packet.get("rules") or []
    ):
        items.append(
            "Regenerate the lockfile (poetry lock / npm install / go mod tidy) "
            "after these manifest edits"
        )
    named = {p.lower() for p in dependency_packages(packet)}
    leftover_lock: list[str] = []
    for signal in detected_signals or []:
        pkg = str(getattr(signal, "package", "") or "").strip()
        ctype = str(getattr(signal, "change_type", "") or "")
        source = str(getattr(signal, "source", "") or "")
        if source != "lockfile" or not pkg:
            continue
        if pkg.lower() in named:
            continue
        if ctype not in {
            "PACKAGE_ADDED",
            "PACKAGE_REMOVED",
            "SDK_MAJOR_BUMP",
            "DEPENDENCY_BUMP",
        }:
            continue
        leftover_lock.append(f"Lockfile also changed `{pkg}` ({ctype}); not in this packet")
    items.extend(leftover_lock[:_REVIEW_CAP])
    for skip in report.skips:
        items.append(skip)
    if generated:
        items.append("Review generated tests: " + ", ".join(generated))
    if corrected:
        items.append("Review self-correct edits: " + ", ".join(corrected))
    if skip_tests:
        items.append("Tests were skipped (--skip-tests); run the suite before merging")
    elif not test_result.passed:
        items.append("Tests failed; do not merge until green")
    if pr_created is False and pr_message:
        items.append(f"PR not opened: {pr_message}")
    return items


def _package_review(
    *,
    package: str,
    packet: dict[str, Any],
    report: PatchReport,
    state: Any,
    coverage: PacketCoverageReport | None,
) -> list[str]:
    pkg = package.lower()
    for mod in load_modules():
        names = {mod.name.lower(), *(p.lower() for p in mod.packages)}
        if pkg not in names:
            continue
        try:
            return list(
                mod.review_checklist(
                    packet=packet,
                    report=report,
                    state=state,
                    coverage=coverage,
                )
                or []
            )
        except Exception:
            return []
    return []


def build_run_summary(
    *,
    packet: dict[str, Any],
    report: PatchReport,
    test_result: TestResult,
    coverage: PacketCoverageReport | None = None,
    state: Any = None,
    generated: list[str] | None = None,
    corrected: list[str] | None = None,
    skip_tests: bool = False,
    pr_created: bool | None = None,
    pr_message: str | None = None,
    detected_signals: list[Any] | None = None,
    audit_log: Any | None = None,
) -> RunSummary:
    package = str(packet.get("package") or "package")
    changes = [_change_line(c) for c in report.changes]
    core = _core_review(
        packet=packet,
        report=report,
        coverage=coverage,
        generated=list(generated or []),
        corrected=list(corrected or []),
        test_result=test_result,
        skip_tests=skip_tests,
        pr_created=pr_created,
        pr_message=pr_message,
        detected_signals=detected_signals,
    )
    score = getattr(audit_log, "score", None) if audit_log is not None else None
    if isinstance(score, dict):
        honesty = score.get("honesty")
        completeness = score.get("migration_completeness")
        notes = score.get("notes") or []
        core.append(
            f"Anti-cheat score (advisory): honesty={honesty} "
            f"completeness={completeness}"
        )
        for note in list(notes)[:5]:
            core.append(f"Anti-cheat note: {note}")
    if audit_log is not None and getattr(audit_log, "entries", None):
        core.append(
            "Migration audit log: .conduit/migration_audit.jsonl "
            f"({len(audit_log.entries)} entries)"
        )
    return RunSummary(
        package=package,
        packet_id=str(packet.get("packet_id") or ""),
        from_version=str(packet.get("from_version") or "?"),
        to_version=str(packet.get("to_version") or "?"),
        files_modified=list(report.files_modified),
        change_lines=_capped(changes, _CHANGE_CAP),
        tests=test_result.summary,
        pr_message=pr_message,
        core_review=core,
        package_review=_package_review(
            package=package,
            packet=packet,
            report=report,
            state=state,
            coverage=coverage,
        ),
        audit_score=score if isinstance(score, dict) else None,
        audit_log_path=".conduit/migration_audit.jsonl" if audit_log else None,
    )


def format_run_summary(summary: RunSummary) -> str:
    files = summary.files_modified
    file_preview = ", ".join(files[:8]) if files else "(none)"
    if len(files) > 8:
        file_preview += f" … +{len(files) - 8} more"

    lines = [
        "=== Run summary ===",
        "",
        "Changed (core)",
        f"- Packet: {summary.packet_id or '(none)'}  "
        f"({summary.from_version} → {summary.to_version})",
        f"- Files ({len(files)}): {file_preview}",
    ]
    if summary.change_lines:
        for line in summary.change_lines:
            lines.append(f"- {line}")
    else:
        lines.append("- (no file-level change details recorded)")
    if summary.tests:
        lines.append(f"- Tests: {summary.tests}")
    if summary.audit_score:
        lines.append(
            "- Anti-cheat score (advisory): "
            f"honesty={summary.audit_score.get('honesty')} "
            f"completeness={summary.audit_score.get('migration_completeness')}"
        )
    if summary.audit_log_path:
        lines.append(f"- Audit log: {summary.audit_log_path}")
    if summary.pr_message:
        lines.append(f"- PR: {summary.pr_message}")

    lines.append("")
    lines.append("Double-check (core)")
    if summary.core_review:
        for item in summary.core_review:
            lines.append(f"- {item}")
    else:
        lines.append("- (nothing flagged)")

    lines.append("")
    lines.append(f"Double-check ({summary.package})")
    if summary.package_review:
        for item in summary.package_review:
            lines.append(f"- {item}")
    else:
        lines.append("- (nothing flagged)")

    return "\n".join(lines)


def format_run_summary_markdown(summary: RunSummary) -> str:
    lines = ["### Review", "", "#### Core"]
    if summary.core_review:
        lines.extend(f"- {item}" for item in summary.core_review)
    else:
        lines.append("- (nothing flagged)")
    lines.extend(["", f"#### {summary.package}"])
    if summary.package_review:
        lines.extend(f"- {item}" for item in summary.package_review)
    else:
        lines.append("- (nothing flagged)")
    return "\n".join(lines)
