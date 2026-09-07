"""Run mechanical anti-cheat (and optionally the LLM auditor)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from conduit.anticheat.findings import (
    AnticheatFinding,
    classify_mechanical_messages,
)
from conduit.anticheat.rules import (
    file_findings,
    is_impl_rel,
    packet_package,
    repo_sdk_import_finding,
)
from conduit.prune.grep_imports import SKIP_DIRS

_SCAN_SUFFIXES = {".py", ".ts", ".js", ".tsx", ".jsx"}
_PROGRESS_EVERY = 25
_SLOW_FILE_SEC = 1.0

LogFn = Callable[[str], None]


def scannable_rels(rels: Iterable[str] | None) -> list[str]:
    """Keep unique posix paths with anti-cheat source suffixes."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in rels or []:
        rel = str(raw or "").replace("\\", "/").strip()
        if not rel or rel in seen:
            continue
        if Path(rel).suffix.lower() not in _SCAN_SUFFIXES:
            continue
        seen.add(rel)
        out.append(rel)
    return out


@dataclass
class AnticheatReport:
    structured: list[AnticheatFinding] = field(default_factory=list)
    advisory: list[AnticheatFinding] = field(default_factory=list)
    source: str = "mechanical"  # mechanical | llm | mixed
    score: dict[str, Any] | None = None

    @property
    def block_findings(self) -> list[AnticheatFinding]:
        return [f for f in self.structured if f.severity == "block"]

    @property
    def findings(self) -> list[str]:
        return [f.to_message() for f in self.block_findings]

    @property
    def messages(self) -> list[str]:
        return self.findings

    @property
    def failed(self) -> bool:
        return bool(self.block_findings)


def _iter_rels(root: Path, files: Iterable[str] | None) -> list[str]:
    root = root.resolve()
    if files is not None:
        from conduit.anticheat.baseline import _repo_rel

        out: list[str] = []
        for r in files:
            rel = _repo_rel(root, r) or str(r).replace("\\", "/")
            if rel:
                out.append(rel)
        return out
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
    log: LogFn | None = None,
    verbose: bool = False,
) -> AnticheatReport:
    root = root.resolve()
    emit: LogFn = log if callable(log) else (lambda _m: None)
    rels = _iter_rels(root, files)
    emit(f"[anticheat] mechanical scan: {len(rels)} file(s)…")
    messages: list[str] = []
    impl_texts: list[tuple[str, str]] = []
    scanned = 0
    for rel in rels:
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        prev = (previous or {}).get(rel)
        t0 = time.perf_counter()
        messages.extend(file_findings(rel, text, packet, previous=prev))
        dt = time.perf_counter() - t0
        scanned += 1
        if verbose and (
            scanned % _PROGRESS_EVERY == 0 or dt >= _SLOW_FILE_SEC
        ):
            emit(
                f"[anticheat] scanned {scanned}/{len(rels)} {rel}"
                + (f" ({dt:.1f}s)" if dt >= _SLOW_FILE_SEC else "")
            )
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
        messages.append(missing)
    seen: set[str] = set()
    uniq: list[str] = []
    for item in messages:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    structured = classify_mechanical_messages(uniq, packet=packet)
    report = AnticheatReport(structured=structured, source="mechanical")
    emit(
        f"[anticheat] mechanical done "
        f"(block={len(report.block_findings)}, "
        f"advisory={len(report.advisory)})"
    )
    return report


def run_anticheat(
    root: Path,
    packet: dict[str, Any],
    files: Iterable[str] | None = None,
    *,
    llm: bool = False,
    previous: dict[str, str] | None = None,
    log: Any | None = None,
    verbose: bool = False,
    audit_log: Any | None = None,
) -> AnticheatReport:
    """Mechanical scan, then optional additive LLM auditor (never subtracts)."""
    report = run_anticheat_mechanical(
        root,
        packet,
        files,
        previous=previous,
        log=log,
        verbose=verbose,
    )
    if audit_log is not None:
        audit_log.record_mechanical(
            report.findings,
            gate="llm" if llm else "mechanical",
        )
        if report.advisory:
            audit_log.record(
                "anticheat_advisory",
                detail="; ".join(f.to_message() for f in report.advisory[:8]),
            )
    if not llm:
        return report
    from conduit.anticheat.llm_audit import llm_audit_findings

    if callable(log):
        try:
            from conduit.pulse import beat

            beat("think")
        except Exception:
            pass
    block, advisory, score = llm_audit_findings(
        root,
        packet,
        audit_log=audit_log,
        files=files,
        mechanical=report.findings,
        mechanical_structured=report.structured,
        log=log,
    )
    if score is not None and audit_log is not None:
        audit_log.score = score
    merged = list(report.structured)
    seen = {f.to_message() for f in merged}
    for item in block:
        msg = item.to_message()
        if msg not in seen:
            seen.add(msg)
            merged.append(item)
    all_advisory = list(report.advisory) + list(advisory)
    return AnticheatReport(
        structured=merged,
        advisory=all_advisory,
        source="mixed" if report.block_findings else "llm",
        score=score,
    )


def anticheat_failure_result(
    findings: list[str] | list[AnticheatFinding],
    *,
    source: str = "mechanical",
    advisory: list[AnticheatFinding] | None = None,
):
    from conduit.test_runner import TestResult

    if findings and isinstance(findings[0], AnticheatFinding):
        block_msgs = [f.to_message() for f in findings if f.severity == "block"]
    else:
        block_msgs = [str(f) for f in findings]
    body = "anticheat failed:\n" + "\n".join(block_msgs)
    if advisory:
        notes = [f.to_message() for f in advisory[:5]]
        if notes:
            body += "\n\nadvisory:\n" + "\n".join(notes)
    return TestResult(
        runner="anticheat",
        passed=False,
        returncode=1,
        stdout=body,
        stderr="",
        command=["conduit", "anticheat"],
        fail_reason=f"implementation cheated tests ({source} anti-cheat)",
    )
