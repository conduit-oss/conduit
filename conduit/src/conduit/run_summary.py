"""End-of-run summary: decision-ready Result / Impact / Anti-cheat / Gaps / Changed / Next."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from conduit.detect.coverage import PacketCoverageReport
from conduit.detect.modules.discovery import load_modules
from conduit.patcher.dependency_update import DEP_RULE_TYPES, dependency_packages
from conduit.patcher.engine import ChangeRecord, PatchReport
from conduit.surface_paths import is_prose_ops_rel
from conduit.test_runner import TestResult

_CHANGE_CAP = 8
_REVIEW_CAP = 12
_GAP_CAP = 10


@dataclass
class RunSummary:
    package: str
    packet_id: str
    from_version: str
    to_version: str
    files_modified: list[str] = field(default_factory=list)
    change_lines: list[str] = field(default_factory=list)
    change_type_counts: dict[str, int] = field(default_factory=dict)
    tests: str = ""
    result_passed: bool = True
    attempts: int | None = None
    pr_message: str | None = None
    # Structured sections
    impact_lines: list[str] = field(default_factory=list)
    anticheat_lines: list[str] = field(default_factory=list)
    gap_lines: list[str] = field(default_factory=list)
    changed_extra: list[str] = field(default_factory=list)
    next_lines: list[str] = field(default_factory=list)
    # Back-compat for older callers / PR body
    core_review: list[str] = field(default_factory=list)
    package_review: list[str] = field(default_factory=list)
    audit_score: dict[str, Any] | None = None
    audit_log_path: str | None = None
    docs_synced: list[str] = field(default_factory=list)

    @property
    def has_review(self) -> bool:
        return bool(
            self.core_review
            or self.package_review
            or self.impact_lines
            or self.anticheat_lines
            or self.gap_lines
        )


def _change_line(change: ChangeRecord) -> str:
    return f"[{change.rule_type}] {change.path}: {change.detail}"


def _capped(items: Iterable[str], cap: int) -> list[str]:
    out = list(items)
    extra = len(out) - cap
    if extra > 0:
        return out[:cap] + [f"... +{extra} more"]
    return out


def _impact_lines(
    *,
    report: PatchReport,
    impact: Any | None,
) -> list[str]:
    """Impact section: fixed successes first; deferred only when unfixed."""
    lines: list[str] = []
    defer_paths: set[str] = set()
    fixed_paths: set[str] = set()
    fixed_labels: list[str] = []

    if impact is not None:
        defer_paths = {
            str(p).replace("\\", "/")
            for p in (getattr(impact, "defer_paths", None) or [])
        }
        for finding in getattr(impact, "findings", []) or []:
            if not isinstance(finding, dict):
                continue
            if str(finding.get("action") or "") != "fix":
                continue
            path = str(finding.get("path") or "").replace("\\", "/")
            kind = str(finding.get("kind") or "impact")
            if path:
                fixed_paths.add(path)
            label = f"{path} ({kind})" if path else kind
            if label not in fixed_labels:
                fixed_labels.append(label)
        for rule in getattr(impact, "post_rules", []) or []:
            if not isinstance(rule, dict):
                continue
            for target in rule.get("target_files") or []:
                path = str(target).replace("\\", "/")
                if path:
                    fixed_paths.add(path)
                    label = f"{path} (post-rule)"
                    # Prefer findings labels; only add if path not already labeled
                    if not any(path in existing for existing in fixed_labels):
                        fixed_labels.append(label)

        if fixed_labels:
            lines.append("fixed: " + ", ".join(_capped(fixed_labels, 6)))
        post_n = len(getattr(impact, "post_rules", []) or [])
        if post_n and not fixed_labels:
            lines.append(f"queued {post_n} impact post-rule(s)")

        unfixed_defer = sorted(defer_paths - fixed_paths)
        if unfixed_defer:
            lines.append(
                "deferred (no fix applied): " + ", ".join(unfixed_defer)
            )

        if getattr(impact, "blocked", False):
            lines.append(
                f"blocked: {getattr(impact, 'block_reason', '') or 'unknown'}"
            )

    covered = fixed_paths | defer_paths
    for skip in report.skips:
        text = str(skip)
        if "deferred impact" not in text.lower():
            continue
        # Skip redundant lines for paths already classified fixed/unfixed
        if any(p in text.replace("\\", "/") for p in covered):
            continue
        if text not in lines:
            lines.append(text)

    if not lines:
        lines.append("(none)")
    return lines


def _other_skips(report: PatchReport) -> list[str]:
    out: list[str] = []
    for skip in report.skips:
        text = str(skip)
        if "deferred impact" in text.lower():
            continue
        out.append(text)
    return out


def _anticheat_lines(
    *,
    audit_log: Any | None,
    advisory: list[Any] | None = None,
) -> list[str]:
    lines: list[str] = []
    score = getattr(audit_log, "score", None) if audit_log is not None else None
    if isinstance(score, dict):
        lines.append(
            "score: "
            f"honesty={score.get('honesty')} "
            f"completeness={score.get('migration_completeness')} "
            "(advisory — does not fail)"
        )
        notes = score.get("notes") or []
        for note in list(notes)[:5]:
            note_s = str(note).strip()
            if not note_s:
                continue
            lines.append(f"decision: {note_s} | action: review")
    adv = list(advisory or [])
    if not adv and audit_log is not None:
        for entry in getattr(audit_log, "entries", []) or []:
            if entry.get("phase") != "anticheat_advisory":
                continue
            detail = str(entry.get("detail") or "").strip()
            if detail:
                lines.append(f"advisory: {detail[:200]} | action: review")
    for item in adv[:8]:
        path = getattr(item, "path", None) or (
            item.get("path") if isinstance(item, dict) else ""
        )
        detail = getattr(item, "detail", None) or (
            item.get("detail") if isinstance(item, dict) else str(item)
        )
        kind = getattr(item, "kind", None) or (
            item.get("kind") if isinstance(item, dict) else "advisory"
        )
        path_s = str(path or "").replace("\\", "/")
        prefix = f"path: {path_s} | " if path_s else ""
        lines.append(f"{prefix}{kind}: {detail} | action: review")
    if not lines:
        lines.append("no block findings; no advisory notes")
    elif not any("no block" in x.lower() for x in lines):
        lines.insert(1 if lines[0].startswith("score:") else 0, "no block findings")
    return lines


def _gap_lines(
    *,
    packet: dict[str, Any],
    coverage: PacketCoverageReport | None,
    detected_signals: list[Any] | None,
    report: PatchReport | None = None,
) -> list[str]:
    items: list[str] = []
    if coverage:
        no_rule_vals = [
            f"`{item.value}`" for item in coverage.no_rule[:_GAP_CAP]
        ]
        if no_rule_vals:
            items.append("NO RULE: " + ", ".join(no_rule_vals))
            leftover = len(coverage.no_rule) - _GAP_CAP
            if leftover > 0:
                items.append(f"... +{leftover} more NO RULE")
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
        items.append(f"Lockfile also changed `{pkg}` ({ctype}); not in this packet")
    if report is not None:
        items.extend(_other_skips(report)[:_REVIEW_CAP])
    return items or ["(none)"]


def _next_lines(
    *,
    test_result: TestResult,
    skip_tests: bool,
    pr_created: bool | None,
    pr_message: str | None,
    generated: list[str],
) -> list[str]:
    items: list[str] = []
    if skip_tests:
        items.append("Tests were skipped (--skip-tests); run the suite before merging")
    elif not test_result.passed:
        items.append("Tests failed; do not merge until green")
    if pr_created is False and pr_message:
        items.append(f"PR not opened: {pr_message}")
    elif pr_message and "fail" in pr_message.lower():
        items.append(pr_message)
    if generated:
        items.append("Review generated tests: " + ", ".join(generated))
    notes = list(getattr(test_result, "extra_notes", None) or [])
    if any("verify_mode=oracle" in n for n in notes) and test_result.passed:
        items.append(
            "Verify ran oracle/smoke only (no consumer venv under the repo); "
            "full pytest was not run"
        )
    if any("verify_kind=missing_dep" in n for n in notes) or (
        test_result.fail_reason or ""
    ).startswith("missing_dep"):
        items.append("Stopped on missing_dep; do not treat this as a leftover-token failure")
    if any("verify_kind=no_consumer_python" in n for n in notes) or (
        test_result.fail_reason or ""
    ).startswith("no_consumer_python"):
        items.append("Stopped on no_consumer_python; full consumer pytest did not run")
    return items or ["(none)"]


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
    impact: Any | None = None,
    advisory: list[Any] | None = None,
    docs_synced: list[str] | None = None,
    attempts: int | None = None,
) -> RunSummary:
    package = str(packet.get("package") or "package")
    changes = [_change_line(c) for c in report.changes]
    type_counts = Counter(
        str(c.rule_type) for c in report.changes if getattr(c, "rule_type", None)
    )
    generated_l = list(generated or [])
    corrected_l = list(corrected or [])
    docs_l = list(docs_synced or [])
    code_corrected = [p for p in corrected_l if not is_prose_ops_rel(p)]
    prose_corrected = [p for p in corrected_l if is_prose_ops_rel(p)]

    impact_section = _impact_lines(report=report, impact=impact)
    anticheat_section = _anticheat_lines(audit_log=audit_log, advisory=advisory)
    gaps = _gap_lines(
        packet=packet,
        coverage=coverage,
        detected_signals=detected_signals,
        report=report,
    )
    next_section = _next_lines(
        test_result=test_result,
        skip_tests=skip_tests,
        pr_created=pr_created,
        pr_message=pr_message,
        generated=generated_l,
    )

    changed_extra: list[str] = []
    if type_counts:
        compact = ", ".join(
            f"{k}×{v}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1])[:8]
        )
        changed_extra.append(f"by rule type: {compact}")
    if code_corrected:
        changed_extra.append(
            "self-correct (code): " + ", ".join(_capped(code_corrected, 8))
        )
    if prose_corrected:
        changed_extra.append(
            "self-correct (prose — unexpected): "
            + ", ".join(_capped(prose_corrected, 6))
        )
    if docs_l:
        changed_extra.append("docs sync: " + ", ".join(_capped(docs_l, 10)))
    if audit_log is not None and getattr(audit_log, "entries", None):
        changed_extra.append(
            f"full detail: .conduit/migration_audit.jsonl "
            f"({len(audit_log.entries)} entries)"
        )

    package_review = _package_review(
        package=package,
        packet=packet,
        report=report,
        state=state,
        coverage=coverage,
    )
    # Back-compat core_review = gaps + next + side notes for old tests
    core_review = list(gaps)
    if generated_l:
        core_review.append("Review generated tests: " + ", ".join(generated_l))
    if corrected_l:
        core_review.append("Review self-correct edits: " + ", ".join(corrected_l))
    if pr_created is False and pr_message:
        core_review.append(f"PR not opened: {pr_message}")
    for line in impact_section:
        if line != "(none)":
            core_review.append(line)

    score = getattr(audit_log, "score", None) if audit_log is not None else None

    return RunSummary(
        package=package,
        packet_id=str(packet.get("packet_id") or ""),
        from_version=str(packet.get("from_version") or "?"),
        to_version=str(packet.get("to_version") or "?"),
        files_modified=list(report.files_modified),
        change_lines=_capped(changes, _CHANGE_CAP),
        change_type_counts=dict(type_counts),
        tests=test_result.summary,
        result_passed=bool(test_result.passed),
        attempts=attempts,
        pr_message=pr_message,
        impact_lines=impact_section,
        anticheat_lines=anticheat_section,
        gap_lines=gaps,
        changed_extra=changed_extra,
        next_lines=next_section,
        core_review=core_review,
        package_review=package_review,
        audit_score=score if isinstance(score, dict) else None,
        audit_log_path=".conduit/migration_audit.jsonl" if audit_log else None,
        docs_synced=docs_l,
    )


def format_run_summary(summary: RunSummary) -> str:
    files = summary.files_modified
    file_preview = ", ".join(files[:8]) if files else "(none)"
    if len(files) > 8:
        file_preview += f" … +{len(files) - 8} more"

    status = "PASSED" if summary.result_passed else "FAILED"
    result = (
        f"Result: {status} | packet {summary.packet_id or '(none)'} "
        f"({summary.from_version} → {summary.to_version})"
    )
    if summary.tests:
        result += f" | {summary.tests}"
    if summary.attempts is not None:
        result += f" | attempts={summary.attempts}"

    lines = [
        "=== Run summary ===",
        result,
        "",
        "Impact",
    ]
    for item in summary.impact_lines:
        lines.append(f"- {item}")

    lines.extend(["", "Anti-cheat"])
    for item in summary.anticheat_lines:
        lines.append(f"- {item}")

    lines.extend(["", "Gaps (coverage)"])
    for item in summary.gap_lines:
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            "Changed",
            f"- Files ({len(files)}): {file_preview}",
        ]
    )
    if summary.change_lines:
        for line in summary.change_lines:
            lines.append(f"- {line}")
    for extra in summary.changed_extra:
        lines.append(f"- {extra}")

    lines.extend(["", "Next"])
    for item in summary.next_lines:
        lines.append(f"- {item}")

    if summary.package_review:
        lines.extend(["", f"Package notes ({summary.package})"])
        for item in summary.package_review:
            lines.append(f"- {item}")

    return "\n".join(lines)


def format_run_summary_markdown(summary: RunSummary) -> str:
    lines = [
        "### Review",
        "",
        "#### Impact",
    ]
    lines.extend(f"- {item}" for item in summary.impact_lines)
    lines.extend(["", "#### Anti-cheat"])
    lines.extend(f"- {item}" for item in summary.anticheat_lines)
    lines.extend(["", "#### Gaps"])
    lines.extend(f"- {item}" for item in summary.gap_lines)
    lines.extend(["", "#### Next"])
    lines.extend(f"- {item}" for item in summary.next_lines)
    if summary.package_review:
        lines.extend(["", f"#### {summary.package}"])
        lines.extend(f"- {item}" for item in summary.package_review)
    # Back-compat heading some tests look for
    if "#### Core" not in "\n".join(lines):
        lines.extend(["", "#### Core"])
        lines.extend(f"- {item}" for item in summary.gap_lines[:5])
    return "\n".join(lines)
