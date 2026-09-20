"""Post-apply leftover scan: consumer calls that still hit removed SDK symbols."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from conduit.export_delta import ExportDelta
from conduit.export_delta.usage import PackageCall, leftover_calls, legacy_resource_calls
from conduit.test_runner import TestResult, annotate_verify

_VERSION_GUARD_RE = re.compile(
    r"__version__|openai\s*<\s*1(?:\.0)?|_openai_has_legacy",
    re.I,
)

_DEPENDENCY_RULE_TYPES = frozenset(
    {"DEPENDENCY_BUMP", "DEPENDENCY_ADD", "DEPENDENCY_REMOVE"}
)


@dataclass(frozen=True)
class Leftover:
    rel: str
    callee: str
    reason: str
    lineno: int = 0

    def display(self) -> str:
        loc = f"{self.rel}:{self.lineno}" if self.lineno else self.rel
        return f"{loc}  {self.callee}  ({self.reason})"


@dataclass(frozen=True)
class ApplyLeftoverVerdict:
    """Outcome of the post-apply leftover gate for ``conduit apply``."""

    status: Literal["clean", "dirty", "pin_only", "incomplete", "unverified"]
    leftovers: tuple[Leftover, ...]
    exit_code: int
    message: str
    binding: object | None = None


def packet_has_call_site_rules(packet: dict) -> bool:
    """True when the packet declares rewrite rules beyond DEPENDENCY_*."""
    for rule in (packet or {}).get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "").strip()
        if not rtype or rtype in _DEPENDENCY_RULE_TYPES:
            continue
        return True
    return False


def scan_packet_leftovers(root: Path, packet: dict) -> list[Leftover]:
    """Collect leftover packet hits. Read-only. Shared by apply + Watch."""
    from conduit.export_delta.usage import collect_package_calls
    from conduit.packet.declaration_rules import DeclarationRuleError, declaration_rules
    from conduit.patcher.declarations import scan_declaration_residuals
    from conduit.patcher.dependency_update import dependency_packages
    from conduit.prune.grep_imports import prune_by_imports

    packages = dependency_packages(packet) or [
        str(packet.get("package") or "").strip()
    ]
    packages = [p for p in packages if p]
    files = prune_by_imports(root, packages) if packages else []
    if not files:
        files = list(root.rglob("*.py"))
    pkg = str(packet.get("package") or (packages[0] if packages else "")).strip()
    calls = collect_package_calls(root, files, pkg) if pkg else []
    leftovers = scan_leftovers(
        root=root,
        calls=calls,
        delta=None,
        packet=packet,
        files=files,
    )

    try:
        decl = declaration_rules(packet)
    except DeclarationRuleError:
        decl = ()
    if decl:
        for path in files:
            if path.suffix.lower() != ".py":
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue
            for residual in scan_declaration_residuals(
                path, text, decl, root=root
            ):
                leftovers.append(
                    Leftover(
                        rel=residual.rel,
                        callee=residual.old_shape,
                        reason=f"{residual.kind}: {residual.reason}",
                        lineno=residual.line,
                    )
                )
    return leftovers


def evaluate_apply_leftovers(*, root: Path, packet: dict) -> ApplyLeftoverVerdict:
    """Gate apply success on leftovers + shared surface binder."""
    from conduit.surface.evaluate import (
        binding_blocks_complete,
        evaluate_packet_binding,
    )
    from conduit.surface.types import VerdictStatus

    if not packet_has_call_site_rules(packet):
        return ApplyLeftoverVerdict(
            status="pin_only",
            leftovers=(),
            exit_code=0,
            message="pin-only: no call-site rules",
        )
    leftovers = tuple(scan_packet_leftovers(root, packet))
    binding = evaluate_packet_binding(root, packet)
    if leftovers:
        detail = "; ".join(item.display() for item in leftovers[:8])
        return ApplyLeftoverVerdict(
            status="dirty",
            leftovers=leftovers,
            exit_code=1,
            message=(
                f"apply incomplete: {len(leftovers)} leftover call(s) remain "
                f"({detail})"
            ),
            binding=binding,
        )
    if binding_blocks_complete(binding):
        status: Literal["incomplete", "unverified"] = (
            "incomplete"
            if binding.status == VerdictStatus.INCOMPLETE
            else "unverified"
        )
        return ApplyLeftoverVerdict(
            status=status,
            leftovers=(),
            exit_code=1,
            message=(
                f"apply {status}: completeness {binding.status.value}: "
                f"{binding.explain()}"
            ),
            binding=binding,
        )
    return ApplyLeftoverVerdict(
        status="clean",
        leftovers=(),
        exit_code=0,
        message="apply complete: no leftover packet calls",
        binding=binding,
    )


def scan_leftovers(
    *,
    root: Path,
    calls: Iterable[PackageCall],
    delta: ExportDelta | None,
    packet: dict | None = None,
    files: Iterable[Path] | None = None,
) -> list[Leftover]:
    """High-severity leftovers: removed-symbol calls + dead version guards."""
    out: list[Leftover] = []
    seen: set[tuple[str, str, str]] = set()
    gone = set(delta.gone_symbols) if delta is not None and not delta.skipped_reason else set()
    if gone:
        for call in leftover_calls(calls, gone):
            key = (call.rel, call.callee, "removed")
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Leftover(
                    rel=call.rel,
                    callee=call.callee,
                    reason="removed on to-pin; not rewritten",
                    lineno=call.lineno,
                )
            )
    paths = (
        delta.resource_paths
        if delta is not None and not delta.skipped_reason
        else {}
    )
    if paths:
        for call in legacy_resource_calls(calls, paths):
            key = (call.rel, call.callee, "removed")
            if key in seen:
                continue
            if any(x.rel == call.rel and x.callee == call.callee for x in out):
                continue
            seen.add(key)
            out.append(
                Leftover(
                    rel=call.rel,
                    callee=call.callee,
                    reason="0.x resource callee still present",
                    lineno=call.lineno,
                )
            )
    for rule in (packet or {}).get("rules") or []:
        if not isinstance(rule, dict) or str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        old = str(rule.get("old_callee") or "").strip()
        if not old:
            continue
        for call in calls:
            if call.callee == old or call.callee.endswith("." + old):
                key = (call.rel, call.callee, "rule")
                if key in seen:
                    continue
                if any(x.rel == call.rel and x.callee == call.callee for x in out):
                    continue
                seen.add(key)
                out.append(
                    Leftover(
                        rel=call.rel,
                        callee=call.callee,
                        reason=f"packet old_callee {old!r} still called",
                        lineno=call.lineno,
                    )
                )
    to_major = _major(str((packet or {}).get("to_version") or ""))
    if to_major is not None and to_major >= 1 and files is not None:
        for leftover in _version_guard_leftovers(root, files):
            key = (leftover.rel, leftover.callee, leftover.reason)
            if key in seen:
                continue
            seen.add(key)
            out.append(leftover)
    return out


def leftovers_failure(items: list[Leftover]) -> TestResult:
    lines = [item.display() for item in items]
    detail = "; ".join(lines[:8])
    result = TestResult(
        runner="leftovers",
        passed=False,
        returncode=1,
        stdout="\n".join(lines),
        stderr="",
        command=[],
        fail_reason=f"incomplete_migration: {detail}",
        extra_notes=["verify_kind=incomplete_migration", "verify_mode=none"],
    )
    return annotate_verify(result, mode="none", kind="incomplete_migration")


def format_leftover_lines(items: Iterable[Leftover]) -> list[str]:
    rows = [item.display() for item in items]
    if not rows:
        return []
    return ["Found, not rewritten:"] + [f"  {row}" for row in rows]


def leftover_handoff_paths(items: Iterable[Leftover]) -> list[str]:
    """Unique consumer paths still incomplete after apply (repair handoff)."""
    return sorted({item.rel for item in items if item.rel})


def _version_guard_leftovers(root: Path, files: Iterable[Path]) -> list[Leftover]:
    root = root.resolve()
    out: list[Leftover] = []
    for path in files:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved.suffix.lower() != ".py":
            continue
        try:
            rel = str(resolved.relative_to(root)).replace("\\", "/")
            text = resolved.read_text(encoding="utf-8-sig")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not _VERSION_GUARD_RE.search(text):
            continue
        if not _has_live_guard(text):
            continue
        out.append(
            Leftover(
                rel=rel,
                callee="__version__ guard",
                reason="version guard still skips modern SDK after bump",
            )
        )
    return out


def _has_live_guard(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bool(_VERSION_GUARD_RE.search(source))
    helpers = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and _helper_is_legacy_version(n)}
    if not helpers and "__version__" not in source:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not _if_early_returns(node):
            continue
        test_src = ast.dump(node.test)
        if "__version__" in test_src:
            return True
        if any(name in test_src for name in helpers):
            return True
    return False


def _helper_is_legacy_version(node: ast.FunctionDef) -> bool:
    dumped = ast.dump(node)
    return "__version__" in dumped and ("lt" in dumped.lower() or "Lt" in dumped)


def _if_early_returns(node: ast.If) -> bool:
    return any(isinstance(stmt, ast.Return) for stmt in node.body)


def _major(version: str) -> int | None:
    head = (version or "").strip().lstrip("vV").split(".", 1)[0]
    if head.isdigit():
        return int(head)
    return None
