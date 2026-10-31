"""Find tests that still import, patch, or mock symbols a release removed.

General mechanism only: given removed/renamed symbols from impact sites and a
repo tree, scan test files for import statements and string patch targets that
name those symbols. Apply reports each hit (file, line, removed symbol); it does
not edit test files here.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from conduit.apply.models import ImpactSite

#: Directory / file name patterns that mark a path as a test module.
_TEST_DIR_NAMES = frozenset({"tests", "test", "testing", "checks", "specs"})
_PATCH_CALLS = frozenset({"patch", "mock", "MagicMock", "AsyncMock", "PropertyMock"})
_STRING_TARGET = re.compile(
    r"""(?:patch|mock\.patch|unittest\.mock\.patch)\s*\(\s*['"]([^'"]+)['"]"""
)


@dataclass(frozen=True)
class TestRef:
    """One place a test file still names a removed API."""

    path: str
    line: int
    symbol: str
    kind: str  # "import" | "patch" | "attr"
    code: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "kind": self.kind,
            "code": self.code,
        }

    def describe(self) -> str:
        return f"{self.path}:{self.line} references removed {self.symbol} ({self.kind})"


def removed_symbols(
    sites: Iterable[ImpactSite],
    *,
    extras: Iterable[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """``(dependency_top, full_symbol)`` for removed or renamed impact symbols.

    ``extras`` carries symbols from impact entries that have no call sites
    (a removed error module only imported in tests still belongs here).
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(top: str, symbol: str) -> None:
        top = (top or "").lower().replace("-", "_")
        if not symbol:
            return
        if not top:
            top = symbol.split(".", 1)[0].lower()
        key = (top, symbol)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    for site in sites:
        if site.kind not in {"removed", "renamed"} or not site.symbol:
            continue
        add(site.dependency, site.symbol)
    for top, symbol in extras or ():
        add(top, symbol)
    return out


def symbols_from_impacts(impacts: Iterable[dict]) -> list[tuple[str, str]]:
    """Removed/renamed symbols from raw impact objects, including empty site lists."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in impacts:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") not in {"removed", "renamed"}:
            continue
        symbol = str(item.get("symbol") or "")
        if not symbol:
            continue
        top = str(item.get("dependency") or "").lower().replace("-", "_")
        if not top:
            top = symbol.split(".", 1)[0].lower()
        key = (top, symbol)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def is_test_path(path: str) -> bool:
    """True when ``path`` looks like a test module (name or parent dir)."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1] if parts else ""
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    return any(part in _TEST_DIR_NAMES for part in parts[:-1])


def find_test_refs(
    root: Path,
    sites: Iterable[ImpactSite],
    *,
    extras: Iterable[tuple[str, str]] | None = None,
) -> list[TestRef]:
    """Every test-file import or string patch that still names a removed symbol."""
    targets = removed_symbols(sites, extras=extras)
    if not targets:
        return []
    found: list[TestRef] = []
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        if not is_test_path(rel):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        found.extend(_refs_in_source(rel, source, targets))
    return found


def _python_files(root: Path) -> list[Path]:
    skip = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".tox"}
    out: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


def _refs_in_source(path: str, source: str, targets: list[tuple[str, str]]) -> list[TestRef]:
    lines = source.splitlines()
    out: list[TestRef] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        out.extend(_import_refs(path, lines, tree, targets))
        out.extend(_ast_patch_refs(path, lines, tree, targets))
        out.extend(_attr_refs(path, lines, tree, targets))
    # String patch targets that sit outside a simple Call (decorators, helpers).
    for match in _STRING_TARGET.finditer(source):
        target = match.group(1)
        line = source.count("\n", 0, match.start()) + 1
        symbol = _match_symbol(target, targets)
        if symbol is None:
            continue
        code = lines[line - 1].strip() if 0 < line <= len(lines) else target
        item = TestRef(path=path, line=line, symbol=symbol, kind="patch", code=code)
        if item not in out:
            out.append(item)
    return out


def _import_refs(
    path: str, lines: list[str], tree: ast.AST, targets: list[tuple[str, str]]
) -> list[TestRef]:
    out: list[TestRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbol = _match_symbol(alias.name, targets)
                if symbol is None:
                    continue
                out.append(
                    TestRef(
                        path=path,
                        line=node.lineno,
                        symbol=symbol,
                        kind="import",
                        code=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else alias.name,
                    )
                )
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            module = node.module
            for alias in node.names:
                candidate = f"{module}.{alias.name}" if alias.name != "*" else module
                symbol = _match_symbol(candidate, targets) or _match_symbol(module, targets)
                if symbol is None:
                    continue
                out.append(
                    TestRef(
                        path=path,
                        line=node.lineno,
                        symbol=symbol,
                        kind="import",
                        code=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else candidate,
                    )
                )
    return out


def _ast_patch_refs(
    path: str, lines: list[str], tree: ast.AST, targets: list[tuple[str, str]]
) -> list[TestRef]:
    out: list[TestRef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_patch_call(node.func):
            continue
        arg = node.args[0] if node.args else None
        if arg is None:
            for keyword in node.keywords:
                if keyword.arg in {None, "target"} and isinstance(keyword.value, ast.Constant):
                    arg = keyword.value
                    break
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            continue
        symbol = _match_symbol(arg.value, targets)
        if symbol is None:
            continue
        line = getattr(arg, "lineno", None) or node.lineno
        out.append(
            TestRef(
                path=path,
                line=line,
                symbol=symbol,
                kind="patch",
                code=lines[line - 1].strip() if 0 < line <= len(lines) else arg.value,
            )
        )
    return out


def _is_patch_call(func: ast.AST) -> bool:
    if isinstance(func, ast.Name):
        return func.id in _PATCH_CALLS or func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr in {"patch", "patch_object"} or func.attr in _PATCH_CALLS
    return False


def _attr_refs(
    path: str, lines: list[str], tree: ast.AST, targets: list[tuple[str, str]]
) -> list[TestRef]:
    """Dotted attribute uses in test code (``pkg.OldClass.acreate(...)``)."""
    out: list[TestRef] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        dotted = _dotted_name(node)
        if dotted is None:
            continue
        symbol = _match_symbol(dotted, targets)
        if symbol is None:
            continue
        line = node.lineno
        key = (line, symbol)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            TestRef(
                path=path,
                line=line,
                symbol=symbol,
                kind="attr",
                code=lines[line - 1].strip() if 0 < line <= len(lines) else dotted,
            )
        )
    return out


def _dotted_name(node: ast.AST) -> str | None:
    """``a.b.c`` for an Attribute chain rooted at a Name; otherwise None."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    parts.reverse()
    return ".".join(parts)


def _match_symbol(text: str, targets: list[tuple[str, str]]) -> str | None:
    """The removed symbol ``text`` names, or None.

    ``text`` is an import module, ``module.Name``, a patch target string, or a
    dotted attribute use in test code.

    Matches:
    - the full symbol, or a path under it on a dot boundary
      (``pkg.OldClass.create`` matches removed ``pkg.OldClass``)
    - a short public alias under the same top package whose class/attr
      segments appear contiguously in the full internal path, including when
      the reference continues past the symbol with more attributes
      (``pkg.Thing.create`` ↔ ``pkg._internal.x.Thing``)
    - a real submodule or class owner of the removed symbol that is more
      than the bare top-level package (``pkg.error`` for ``pkg.error.Gone``)

    Does not match:
    - a bare ``import pkg`` / patch of just the top package against a removed
      attribute like ``pkg.api_base``
    - a sibling under the same owner (``pkg.Widget.build`` vs ``pkg.Widget.create``)
    - a longer name that only shares a prefix without a dot
      (``pkg.OldClassX`` does not match ``pkg.OldClass``)
    """
    if not text:
        return None
    named = text.replace("-", "_")
    parts = named.split(".")
    for top, symbol in targets:
        sym_parts = symbol.split(".")
        # Exact symbol, or reference that continues past it on a dot boundary.
        if named == symbol or named.startswith(symbol + "."):
            return symbol
        if parts[0] != top:
            continue
        # Bare top-level package alone never matches a deeper removed symbol.
        if named == top:
            if symbol == top:
                return symbol
            continue
        if len(parts) >= 2 and len(sym_parts) >= 2:
            tail = parts[1:]
            sym_tail = sym_parts[1:]
            # Named is a short alias of (or shorter than) the full symbol.
            n = len(tail)
            for start in range(len(sym_tail) - n + 1):
                if sym_tail[start : start + n] == tail:
                    return symbol
            # Named continues past the symbol: a leaf path of the symbol is a
            # prefix of named's tail (``OldClass.create`` vs removed ``OldClass``
            # at ``pkg._internal.OldClass``). Match only whole segments.
            for leaf_start in range(len(sym_tail)):
                leaf = sym_tail[leaf_start:]
                if leaf and len(leaf) <= len(tail) and tail[: len(leaf)] == leaf:
                    return symbol
        # Owner / submodule prefix longer than the top package only.
        # ``pkg.error`` owns ``pkg.error.Gone``; ``pkg`` alone does not own
        # ``pkg.api_base``. Exact owner only: ``pkg.Widget.build`` must not
        # match a removed ``pkg.Widget.create`` via the ``pkg.Widget`` owner.
        for length in range(len(sym_parts) - 1, 1, -1):
            owner = ".".join(sym_parts[:length])
            if named == owner:
                return symbol
    return None
