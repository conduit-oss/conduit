"""Python consumer index: imports, call/decorator chains, local bases."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Sequence

from conduit.surface.types import ConsumerIndex, FileIndex, SourceSpan, UseKind, UseSite


def index_python(root: Path, files: Sequence[Path]) -> ConsumerIndex:
    """Build a package-agnostic ConsumerIndex from Python sources."""
    root = root.resolve()
    indexed: list[FileIndex] = []
    for path in files:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved.suffix.lower() != ".py":
            continue
        try:
            rel = str(resolved.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = resolved.name
        try:
            text = resolved.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        file_index = _index_source(rel, text)
        if file_index is not None:
            indexed.append(file_index)
    return ConsumerIndex(files=tuple(indexed))


def _index_source(rel: str, source: str) -> FileIndex | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    aliases = _collect_aliases(tree)
    local_bases = _collect_bases(tree, aliases)
    annotations = _collect_annotations(tree)
    constructors = _collect_constructors(tree)
    uses = _collect_uses(tree, rel)
    return FileIndex(
        rel=rel,
        aliases=aliases,
        local_bases=local_bases,
        annotations=annotations,
        constructors=constructors,
        uses=tuple(uses),
    )


def _collect_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                aliases[local] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                origin = f"{module}.{alias.name}" if module else alias.name
                aliases[local] = origin
    return aliases


def _collect_bases(tree: ast.AST, aliases: dict[str, str]) -> dict[str, tuple[str, ...]]:
    bases: dict[str, tuple[str, ...]] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.ClassDef):
            continue
        resolved: list[str] = []
        for base in node.bases:
            chain = _attr_chain(base)
            if not chain:
                continue
            root = chain.split(".", 1)[0]
            if root in aliases:
                rest = chain[len(root) :]
                resolved.append(aliases[root] + rest)
            else:
                resolved.append(chain)
        bases[node.name] = tuple(resolved)
    return bases


def _collect_annotations(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            chain = _attr_chain(node.annotation) if node.annotation else None
            if chain:
                out[node.target.id] = chain
        elif isinstance(node, ast.FunctionDef):
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                if arg.annotation and isinstance(arg.arg, str):
                    chain = _attr_chain(arg.annotation)
                    if chain:
                        out[arg.arg] = chain
    return out


def _collect_constructors(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        chain = _attr_chain(node.value.func)
        if chain:
            out[target.id] = chain
    return out


def _collect_uses(tree: ast.AST, rel: str) -> list[UseSite]:
    uses: list[UseSite] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            enclosing = self._class_stack[-1] if self._class_stack else None
            for dec in node.decorator_list:
                self._add_decorator(dec, enclosing)
            self._class_stack.append(node.name)
            for child in node.body:
                self.visit(child)
            self._class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            enclosing = self._class_stack[-1] if self._class_stack else None
            for dec in node.decorator_list:
                self._add_decorator(dec, enclosing)
            for child in node.body:
                self.visit(child)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            enclosing = self._class_stack[-1] if self._class_stack else None
            for dec in node.decorator_list:
                self._add_decorator(dec, enclosing)
            for child in node.body:
                self.visit(child)

        def visit_Call(self, node: ast.Call) -> None:
            chain = _attr_chain(node.func)
            if chain:
                uses.append(
                    UseSite(
                        span=SourceSpan(
                            path=rel,
                            line=getattr(node, "lineno", 0) or 0,
                            col=getattr(node, "col_offset", 0) or 0,
                        ),
                        use_kind=UseKind.CALL,
                        chain=chain,
                        enclosing_class=(
                            self._class_stack[-1] if self._class_stack else None
                        ),
                    )
                )
            self.generic_visit(node)

        def _add_decorator(self, dec: ast.AST, enclosing: str | None) -> None:
            expr = dec.func if isinstance(dec, ast.Call) else dec
            chain = _attr_chain(expr)
            if not chain:
                return
            uses.append(
                UseSite(
                    span=SourceSpan(
                        path=rel,
                        line=getattr(dec, "lineno", 0) or 0,
                        col=getattr(dec, "col_offset", 0) or 0,
                    ),
                    use_kind=UseKind.DECORATOR,
                    chain=chain,
                    enclosing_class=enclosing,
                )
            )

    _Visitor().visit(tree)
    return uses


def _attr_chain(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None
