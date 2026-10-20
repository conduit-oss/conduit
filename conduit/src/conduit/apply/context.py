"""Build a per-site context pack from source + impact evidence."""

from __future__ import annotations

import ast
from pathlib import Path

from conduit.apply.models import ContextPack, ImpactSite


class ContextError(ValueError):
    """Raised when the flagged line has no enclosing function."""


def build_context_pack(root: Path, site: ImpactSite) -> ContextPack:
    file_path = root / site.path
    if not file_path.is_file():
        raise ContextError(f"file not found: {site.path}")
    source = file_path.read_text(encoding="utf-8")
    span = enclosing_function(source, site.line) or module_statement(source, site.line)
    if span is None:
        raise ContextError(f"no enclosing function at {site.path}:{site.line}")
    imports = module_imports(source)
    call_kwargs = call_kwargs_at_line(source, site.line)
    release_diff = {
        "dependency": site.dependency,
        "from_version": site.from_version,
        "to_version": site.to_version,
        "change": site.change,
        "confidence": site.confidence,
        "source": site.source,
        "kind": site.kind,
        "symbol": site.symbol,
        "old": site.old_signature,
        "new": site.new_signature,
        "call_kwargs": list(call_kwargs),
    }
    if site.evidence:
        release_diff["evidence"] = site.evidence
    candidates = [dict(item) for item in site.candidates]
    if candidates:
        release_diff["candidates"] = candidates
    return ContextPack(
        site=site,
        file_path=site.path,
        imports=imports,
        function_name=span[0],
        function_source=span[1],
        function_start_line=span[2],
        function_end_line=span[3],
        full_source=source,
        release_diff=release_diff,
        new_signature=site.new_signature,
        new_docstring=site.new_docstring,
        candidates=candidates,
    )


def enclosing_function(
    source: str, line: int
) -> tuple[str, str, int, int] | None:
    """Return ``(name, source_slice, start_line, end_line)`` for the innermost def/async def."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ContextError(f"cannot parse source: {exc}") from exc
    lines = source.splitlines(keepends=True)
    best: ast.AST | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is None or end is None:
            continue
        if start <= line <= end:
            if best is None:
                best = node
                continue
            b_start = getattr(best, "lineno", 0)
            b_end = getattr(best, "end_lineno", 10**9)
            # Prefer the innermost (smallest) span.
            if (end - start) < (b_end - b_start):
                best = node
    if best is None:
        return None
    assert isinstance(best, (ast.FunctionDef, ast.AsyncFunctionDef))
    start = int(best.lineno)
    end = int(best.end_lineno or best.lineno)
    slice_src = "".join(lines[start - 1 : end])
    return best.name, slice_src, start, end


def module_statement(source: str, line: int) -> tuple[str, str, int, int] | None:
    """A module-level statement (not a class) spanning ``line``; name is empty."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in tree.body:
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        if start <= line <= end:
            if isinstance(node, ast.ClassDef):
                return None
            return "", "".join(lines[start - 1 : end]), start, end
    return None


def module_imports(source: str) -> str:
    """Return top-level import statements (text), for the context pack."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines = source.splitlines(keepends=True)
    chunks: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            start = node.lineno
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            chunks.append("".join(lines[start - 1 : end]).rstrip())
    return "\n".join(chunks)


def call_kwargs_at_line(source: str, line: int) -> tuple[str, ...]:
    """Keyword argument names on the call spanning ``line`` (1-based), if any."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    best: ast.Call | None = None
    best_span: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        if start is None or end is None:
            continue
        if start <= line <= end:
            span = end - start
            if best is None or span < (best_span or 10**9):
                best = node
                best_span = span
    if best is None:
        return ()
    names = tuple(
        keyword.arg
        for keyword in best.keywords
        if isinstance(keyword.arg, str) and keyword.arg
    )
    return names
