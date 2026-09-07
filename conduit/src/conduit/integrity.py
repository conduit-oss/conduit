"""Mechanical integrity audit — does not trust pytest exit codes.

Flags dummy exception handlers, unused migration-marker tuples, leftover
obfuscation, and skip/xfail in Conduit-generated tests. Self-correct may only
fix implementation by using the real new API.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Iterable

from conduit.test_gen import is_conduit_generated_rel

_MARKER_NAME_RE = re.compile(
    r"(MARKER|MIGRATION|REQUIRED_TOKENS|REQUIRED_SHAPES)",
    re.I,
)
_DUMMY_CONST = {"", "ok", "OK", None, 0, False}


def dotted_expr(node: ast.AST) -> str:
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return ".".join(parts)


def py_used_callees(text: str) -> set[str]:
    """Dotted names that appear as Attribute or Call targets."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    found: set[str] = set()

    class _V(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            dotted = dotted_expr(node.func)
            if dotted:
                found.add(dotted)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            dotted = dotted_expr(node)
            if dotted:
                found.add(dotted)
            self.generic_visit(node)

    _V().visit(tree)
    return found


def callee_used_in_text(text: str, callee: str) -> bool:
    callee = (callee or "").strip()
    if not callee:
        return False
    used = py_used_callees(text)
    if callee in used:
        return True
    for item in used:
        if item.endswith("." + callee) or callee.endswith("." + item):
            return True
        if callee in item.split("."):
            # too loose for "create" — require last segment match
            pass
    last = callee.split(".")[-1]
    for item in used:
        parts = item.split(".")
        if parts and parts[-1] == last and callee.replace(" ", "") in item.replace(" ", ""):
            return True
        if item.endswith(callee):
            return True
    # JS/TS or unparsable Python: require non-string call-ish use
    if f"{callee}(" in text or f"{callee} (" in text:
        return True
    return False


def _name_referenced(tree: ast.AST, name: str, skip_assign: ast.AST | None) -> bool:
    class _V(ast.NodeVisitor):
        found = False

        def visit_Name(self, node: ast.Name) -> None:
            if node.id == name and node is not skip_assign:
                if isinstance(node.ctx, ast.Load):
                    self.found = True
            self.generic_visit(node)

    v = _V()
    v.visit(tree)
    return v.found


def unused_marker_literals(text: str, interesting: Iterable[str]) -> list[str]:
    """String literals stored in unused MARKER/MIGRATION tuples."""
    want = {s for s in interesting if s}
    if not want:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not node.targets or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if not _MARKER_NAME_RE.search(name):
            continue
        if _name_referenced(tree, name, skip_assign=node.targets[0]):
            continue
        values: list[str] = []
        val = node.value
        seq: ast.AST | None = val
        if isinstance(val, ast.Tuple | ast.List | ast.Set):
            seq = val
        if isinstance(seq, (ast.Tuple, ast.List, ast.Set)):
            for elt in seq.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    values.append(elt.value)
        elif isinstance(val, ast.Constant) and isinstance(val.value, str):
            values.append(val.value)
        for item in values:
            if item in want:
                hits.append(f"{name}={item!r}")
    return hits


def _is_dummy_value(node: ast.AST | None) -> bool:
    """True only for trivial stub sinks (not legitimate returns/calls)."""
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value in _DUMMY_CONST
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not getattr(
        node, "elts", None
    ):
        return True
    if isinstance(node, ast.BoolOp):
        # Catch `x or "ok"` / `x or {}` stub fallbacks; not every BoolOp.
        has_dummy_operand = False
        for v in node.values:
            if isinstance(v, ast.Constant) and v.value in _DUMMY_CONST:
                has_dummy_operand = True
            elif isinstance(v, ast.Dict) and not v.keys:
                has_dummy_operand = True
            elif isinstance(v, (ast.List, ast.Tuple, ast.Set)) and not getattr(
                v, "elts", None
            ):
                has_dummy_operand = True
        return has_dummy_operand and any(_is_dummy_value(v) for v in node.values)
    return False


def _handler_is_blanket(node: ast.ExceptHandler) -> bool:
    if node.type is None:
        return True
    if isinstance(node.type, ast.Name) and node.type.id in {
        "Exception",
        "BaseException",
    }:
        return True
    if isinstance(node.type, ast.Tuple):
        names = [
            elt.id for elt in node.type.elts if isinstance(elt, ast.Name)
        ]
        if names and all(n in {"Exception", "BaseException"} for n in names):
            return True
    return False


def dummy_except_findings(
    text: str, rel: str, package: str = ""
) -> list[str]:
    """Flag blanket excepts that swallow an SDK call with a dummy return.

    When ``package`` is set, only try bodies that invoke that SDK (incl. client
    aliases) are considered. Without a package, keeps the legacy whole-file scan
    used by post-rule body validation.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    roots: set[str] | None = None
    pkg = (package or "").strip()
    if pkg:
        from conduit.anticheat.rules import _sdk_name_roots

        roots = _sdk_name_roots(tree, pkg)
    findings: list[str] = []

    class _V(ast.NodeVisitor):
        def visit_Try(self, node: ast.Try) -> None:
            for handler in node.handlers:
                if not _handler_is_blanket(handler):
                    continue
                raises = any(isinstance(s, ast.Raise) for s in ast.walk(handler))
                if raises:
                    continue
                if roots is not None:
                    from conduit.anticheat.rules import _try_body_calls_package

                    if not _try_body_calls_package(node, pkg, roots):
                        continue
                dummy = False
                for stmt in handler.body:
                    if isinstance(stmt, ast.Pass):
                        dummy = True
                    elif isinstance(stmt, ast.Return) and _is_dummy_value(stmt.value):
                        dummy = True
                    elif isinstance(stmt, ast.Expr) and isinstance(
                        stmt.value, ast.Constant
                    ):
                        continue
                if dummy:
                    findings.append(
                        f"{rel}:{handler.lineno} swallows Exception and returns a dummy"
                    )
            self.generic_visit(node)

    _V().visit(tree)
    return findings


def packet_new_tokens(packet: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        for key in (
            "replace",
            "new_callee",
            "new_param",
            "new_attr",
            "new_import",
            "new_key",
        ):
            val = str(rule.get(key) or "").strip()
            if val and val not in seen:
                seen.add(val)
                out.append(val)
    return out


def audit_consumer_tests(root: Path) -> list[str]:
    """Notes about existing tests that should not be trusted as coverage."""
    notes: list[str] = []
    tests = root / "tests"
    paths: list[Path] = []
    if tests.is_dir():
        paths.extend(tests.rglob("test_*.py"))
        paths.extend(tests.rglob("conftest.py"))
    conftest = root / "conftest.py"
    if conftest.is_file():
        paths.append(conftest)
    for path in paths:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if is_conduit_generated_rel(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.lower()
        if "pytest.mark.skip" in lowered or "pytest.mark.xfail" in lowered:
            notes.append(f"{rel} uses skip/xfail — do not treat as migration coverage")
        if "assert true" in lowered:
            notes.append(f"{rel} has tautological assert True")
        if "pytest.skip" in text and "OPENAI_API_KEY" in text:
            notes.append(
                f"{rel} skips without OPENAI_API_KEY — Conduit must supply the key"
            )
    return notes


def integrity_findings(
    root: Path,
    packet: dict[str, Any],
    files: Iterable[str] | None = None,
) -> list[str]:
    """Scan impl + generated tests. Any finding is a verify failure."""
    from conduit.anticheat.scan import run_anticheat_mechanical

    return run_anticheat_mechanical(root, packet, files).messages


def integrity_failure_result(findings: list[str]):
    """Build a TestResult-shaped failure for the self-correct loop."""
    from conduit.anticheat.scan import anticheat_failure_result

    return anticheat_failure_result(findings, source="mechanical")
