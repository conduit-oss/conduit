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

from conduit.prune.grep_imports import SKIP_DIRS
from conduit.test_gen import (
    CONDUIT_GENERATED_NAMES,
    is_conduit_generated_rel,
    oracle_forbidden_tokens,
)
from conduit.text_tokens import obfuscated_forbidden_tokens, token_in_text

_MARKER_NAME_RE = re.compile(
    r"(MARKER|MIGRATION|REQUIRED_TOKENS|REQUIRED_SHAPES)",
    re.I,
)
_SKIP_NEEDLES = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "unittest.skip",
    "unittest.expectedFailure",
)
_DUMMY_CONST = {"", "ok", "OK", None, 0, False}


def _is_test_rel(rel: str) -> bool:
    posix = rel.replace("\\", "/").lower()
    name = Path(posix).name
    return (
        posix.startswith("tests/")
        or "/tests/" in posix
        or name.startswith("test_")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or name == "conftest.py"
    )


def _is_impl_rel(rel: str) -> bool:
    if _is_test_rel(rel):
        return False
    name = Path(rel.replace("\\", "/")).name
    if name in {
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "constraints.txt",
        "go.mod",
    }:
        return False
    return True


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
    if node is None:
        return True
    if isinstance(node, ast.Constant) and node.value in _DUMMY_CONST:
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Dict)) and (
        (isinstance(node, ast.Dict) and not node.keys)
        or (hasattr(node, "elts") and not getattr(node, "elts", None))
    ):
        return True
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.BoolOp):
        return all(_is_dummy_value(v) for v in node.values) or True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Call):
        return True
    if isinstance(node, ast.BinOp):
        return True
    if isinstance(node, ast.Attribute):
        return True
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


def dummy_except_findings(text: str, rel: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    findings: list[str] = []

    class _V(ast.NodeVisitor):
        def visit_Try(self, node: ast.Try) -> None:
            for handler in node.handlers:
                if not _handler_is_blanket(handler):
                    continue
                raises = any(isinstance(s, ast.Raise) for s in ast.walk(handler))
                if raises:
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


def _generated_skip_findings(rel: str, text: str) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    if any(n in lowered for n in _SKIP_NEEDLES) or ".skip(" in lowered or ".xfail(" in lowered:
        hits.append(f"{rel} weakens generated tests with skip/xfail")
    return hits


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
    root = root.resolve()
    forbidden = oracle_forbidden_tokens(packet)
    interesting = packet_new_tokens(packet)
    rels: list[str] = []
    if files:
        rels = [str(r).replace("\\", "/") for r in files]
    else:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in {".py", ".ts", ".js", ".tsx", ".jsx"}:
                continue
            try:
                rels.append(path.relative_to(root).as_posix())
            except ValueError:
                continue

    findings: list[str] = []
    for rel in rels:
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if is_conduit_generated_rel(rel):
            findings.extend(_generated_skip_findings(rel, text))
            continue
        if _is_impl_rel(rel):
            hidden = obfuscated_forbidden_tokens(text, forbidden)
            if hidden:
                findings.append(
                    f"{rel} obfuscates leftover tokens via concat/join: "
                    + ", ".join(hidden)
                )
            findings.extend(dummy_except_findings(text, rel))
            markers = unused_marker_literals(text, interesting)
            if markers:
                findings.append(
                    f"{rel} unused migration marker literals: " + ", ".join(markers)
                )
            # JS join obfuscation already covered by obfuscated_forbidden_tokens
        if _is_test_rel(rel) and Path(rel).name in CONDUIT_GENERATED_NAMES:
            findings.extend(_generated_skip_findings(rel, text))
    return findings


def integrity_failure_result(findings: list[str]):
    """Build a TestResult-shaped failure for the self-correct loop."""
    from conduit.test_runner import TestResult

    body = "integrity audit failed:\n" + "\n".join(findings)
    return TestResult(
        runner="integrity",
        passed=False,
        returncode=1,
        stdout=body,
        stderr="",
        command=["conduit", "integrity-audit"],
        fail_reason="implementation cheated tests (integrity audit)",
    )
