"""Run mechanical anti-cheat (and optionally the LLM auditor)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from conduit.anticheat.rules import (
    file_findings,
    is_impl_rel,
    packet_package,
    repo_sdk_import_finding,
)
from conduit.prune.grep_imports import SKIP_DIRS

_SCAN_SUFFIXES = {".py", ".ts", ".js", ".tsx", ".jsx"}


@dataclass
class AnticheatReport:
    findings: list[str] = field(default_factory=list)
    source: str = "mechanical"  # mechanical | llm | mixed
    score: dict[str, Any] | None = None

    @property
    def messages(self) -> list[str]:
        return list(self.findings)

    @property
    def failed(self) -> bool:
        return bool(self.findings)


def _iter_rels(root: Path, files: Iterable[str] | None) -> list[str]:
    root = root.resolve()
    if files:
        return [str(r).replace("\\", "/") for r in files]
    rels: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _SCAN_SUFFIXES:
            continue
        try:
            rels.append(path.relative_to(root).as_posix())
        except ValueError:
            continue
    return rels


def run_anticheat_mechanical(
    root: Path,
    packet: dict[str, Any],
    files: Iterable[str] | None = None,
    *,
    previous: dict[str, str] | None = None,
) -> AnticheatReport:
    root = root.resolve()
    findings: list[str] = []
    impl_texts: list[tuple[str, str]] = []
    for rel in _iter_rels(root, files):
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        prev = (previous or {}).get(rel)
        findings.extend(file_findings(rel, text, packet, previous=prev))
        if is_impl_rel(rel):
            impl_texts.append((rel, text))

    pkg = packet_package(packet)
    pin = None
    try:
        from conduit.detect.manifests import (
            pin_for_packet_ecosystem,
            read_installed_by_ecosystem,
        )

        by_eco = read_installed_by_ecosystem(root)
        pin = pin_for_packet_ecosystem(by_eco, pkg, packet.get("ecosystem"))
    except Exception:
        pin = None
    missing = repo_sdk_import_finding(
        package=pkg,
        ecosystem=str(packet.get("ecosystem") or ""),
        pin=pin,
        impl_texts=impl_texts,
        packet=packet,
    )
    if missing:
        findings.append(missing)
    seen: set[str] = set()
    uniq: list[str] = []
    for item in findings:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return AnticheatReport(findings=uniq, source="mechanical")


def run_anticheat(
    root: Path,
    packet: dict[str, Any],
    files: Iterable[str] | None = None,
    *,
    llm: bool = False,
    previous: dict[str, str] | None = None,
    log: Any | None = None,
    audit_log: Any | None = None,
) -> AnticheatReport:
    """Mechanical scan, then optional additive LLM auditor (never subtracts)."""
    report = run_anticheat_mechanical(
        root, packet, files, previous=previous
    )
    if audit_log is not None:
        audit_log.record_mechanical(
            report.findings,
            gate="llm" if llm else "mechanical",
        )
    if not llm:
        return report
    from conduit.anticheat.llm_audit import llm_audit_findings

    extra, score = llm_audit_findings(
        root,
        packet,
        audit_log=audit_log,
        files=files,
        mechanical=report.findings,
        log=log,
    )
    if score is not None and audit_log is not None:
        audit_log.score = score
    if extra:
        merged = list(report.findings)
        for item in extra:
            if item not in merged:
                merged.append(item)
        return AnticheatReport(
            findings=merged,
            source="mixed" if report.findings else "llm",
            score=score,
        )
    return AnticheatReport(
        findings=list(report.findings),
        source=report.source,
        score=score,
    )


def anticheat_failure_result(findings: list[str], *, source: str = "mechanical"):
    from conduit.test_runner import TestResult

    body = "anticheat failed:\n" + "\n".join(findings)
    return TestResult(
        runner="anticheat",
        passed=False,
        returncode=1,
        stdout=body,
        stderr="",
        command=["conduit", "anticheat"],
        fail_reason=f"implementation cheated tests ({source} anti-cheat)",
    )
