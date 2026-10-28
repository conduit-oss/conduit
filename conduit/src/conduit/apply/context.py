"""Build a per-site context pack from source + impact evidence."""

from __future__ import annotations

import ast
from pathlib import Path

from conduit.apply.dictkeys import key_hints
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
    names = removed_names(source, site)
    uses = name_uses(source, names, (span[2], span[3])) if names else []
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
        decorators=function_decorators(source, span[2]) if span[0] else [],
        removed_name_uses=uses,
        dict_key_hints=key_hints(root, site.path, source, (span[2], span[3])),
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


def function_decorators(source: str, def_line: int) -> list[str]:
    """Decorators of the def that starts at ``def_line``, as ``ast.unparse`` text."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno == def_line:
            return [ast.unparse(deco) for deco in node.decorator_list]
    return []


def removed_names(source: str, site: ImpactSite) -> set[str]:
    """Names this file imports from the dependency that the site's change removes.

    The removed symbol's last part (``pkg.types.mod.Thing`` -> ``Thing``) when
    the file imports exactly that name from the dependency's package."""
    if site.kind not in {"removed", "renamed"} or not site.symbol:
        return set()
    tail = site.symbol.rsplit(".", 1)[-1]
    top = site.dependency.lower().replace("-", "_")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] == top:
            for alias in node.names:
                if alias.name == tail:
                    out.add(alias.asname or alias.name)
    return out


def name_uses(source: str, names: set[str], span: tuple[int, int]) -> list[dict]:
    """Every line that still uses one of ``names`` (string annotations included),
    other than the import that binds it. A use as a parameter or return
    annotation names that function, so the type that now reaches it can be found."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    imports = {
        number
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for number in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    found: dict[tuple[str, int], dict] = {}

    def mentions(node: ast.AST) -> set[str]:
        hit: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in names:
                hit.add(sub.id)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                try:
                    hit |= mentions(ast.parse(sub.value.strip(), mode="eval"))
                except SyntaxError:
                    pass
        return hit

    def add(name: str, line: int, **where: str) -> None:
        if line in imports or not 0 < line <= len(lines):
            return
        item = found.setdefault((name, line), {
            "name": name,
            "line": line,
            "code": lines[line - 1].strip(),
            "in_this_function": span[0] <= line <= span[1],
        })
        for key, value in where.items():
            item.setdefault(key, value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for arg in params:
                if arg.annotation is not None:
                    for name in mentions(arg.annotation):
                        add(name, arg.annotation.lineno, function=node.name, param=arg.arg)
            if node.returns is not None:
                for name in mentions(node.returns):
                    add(name, node.returns.lineno, function=node.name, returns="yes")
        elif isinstance(node, ast.AnnAssign):
            for name in mentions(node.annotation):
                add(name, node.annotation.lineno)
        elif isinstance(node, ast.Name) and node.id in names:
            add(node.id, node.lineno)
    return sorted(found.values(), key=lambda item: (item["line"], item["name"]))
