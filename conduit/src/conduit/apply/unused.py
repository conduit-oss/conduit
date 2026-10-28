"""Drop a ``from x import name`` that a rewrite left unused in the file it touched."""

from __future__ import annotations

import ast
import re
from pathlib import Path


def drop_unused_imports(
    before: str, after: str, span: tuple[int, int], path: str = "", root: Path | None = None
) -> tuple[str, tuple[int, int]]:
    """``after`` without names the rewrite stopped using, and ``span`` moved to match.

    Only a top-level, one-line ``from ... import`` above the span is touched, and
    only for a name the file used before the rewrite that no longer appears
    anywhere else in the file (code, strings or comments). A package
    ``__init__.py`` keeps its imports, and so does any module another file
    under ``root`` imports that name from: they are re-exports."""
    if path.endswith("__init__.py"):
        return after, span
    try:
        old_tree, new_tree = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return after, span
    if any(
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        for node in new_tree.body
    ):
        return after, span
    lines = after.splitlines(keepends=True)
    removed_above = 0
    for stmt in sorted(new_tree.body, key=lambda node: node.lineno, reverse=True):
        if (
            not isinstance(stmt, ast.ImportFrom)
            or stmt.module == "__future__"
            or stmt.lineno != stmt.end_lineno
            or stmt.end_lineno >= span[0]
            or any(alias.name == "*" for alias in stmt.names)
        ):
            continue
        line = lines[stmt.lineno - 1]
        if line[: stmt.col_offset].strip() or line[stmt.end_col_offset :].strip() not in {"", "\\"} and not line[stmt.end_col_offset :].strip().startswith("#"):
            continue
        rest = "".join(lines[: stmt.lineno - 1] + lines[stmt.lineno :])
        old_rest = _without_imports(before, old_tree)
        unused = [
            alias
            for alias in stmt.names
            if not _mentions(rest, alias.asname or alias.name)
            and _mentions(old_rest, alias.asname or alias.name)
            and not _imported_elsewhere(root, path, alias.asname or alias.name)
        ]
        if not unused:
            continue
        kept = [alias for alias in stmt.names if alias not in unused]
        if kept:
            text = ast.unparse(ast.ImportFrom(module=stmt.module, names=kept, level=stmt.level))
            lines[stmt.lineno - 1] = line[: stmt.col_offset] + text + line[stmt.end_col_offset :]
        else:
            del lines[stmt.lineno - 1]
            removed_above += 1
    if not removed_above and "".join(lines) == after:
        return after, span
    return "".join(lines), (span[0] - removed_above, span[1] - removed_above)


def _without_imports(source: str, tree: ast.Module) -> str:
    lines = source.splitlines(keepends=True)
    drop = {
        number
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for number in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return "".join(line for number, line in enumerate(lines, 1) if number not in drop)


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", text) is not None or re.search(
        rf"\.{re.escape(name)}(?!\w)", text
    ) is not None


def _imported_elsewhere(root: Path | None, path: str, name: str) -> bool:
    """Another file imports ``name`` from this module (by its last dotted part)."""
    if root is None or not path:
        return False
    stem = Path(path).stem
    pattern = re.compile(rf"from\s+[\w.]*\b{re.escape(stem)}\s+import\s[^\n]*(?<![\w.]){re.escape(name)}(?!\w)")
    for file in Path(root).rglob("*.py"):
        if file.relative_to(root).as_posix() == path or any(
            part.startswith(".") or part in {"venv", "node_modules"} for part in file.relative_to(root).parts
        ):
            continue
        try:
            if pattern.search(file.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False
