"""Splice a rewritten function (+ optional imports) into a source file."""

from __future__ import annotations

import ast
import difflib
import json
import re
import textwrap
from dataclasses import dataclass

from conduit.apply.models import ContextPack


class PatchError(ValueError):
    pass


@dataclass(frozen=True)
class ModelEdit:
    function_source: str
    new_imports: tuple[str, ...]
    #: ``(line, old line text, new line text)`` for lines outside the rewritten
    #: function that still use a name the change removes. ``old`` is filled in
    #: by ``resolve_line_edits`` so a later rebuild finds the line by its text.
    line_edits: tuple[tuple[int, str, str], ...] = ()


def parse_model_edit(text: str) -> ModelEdit:
    """Parse ``function_source`` + ``new_imports`` from JSON or fenced blocks."""
    raw = (text or "").strip()
    if not raw:
        raise PatchError("model reply is empty")

    candidate = raw
    fenced_json = re.search(r"```(?:json)?\s*\n(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fenced_json:
        candidate = fenced_json.group(1).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "function_source" in data:
        imports = data.get("new_imports") or []
        if isinstance(imports, str):
            import_lines = tuple(line.strip() for line in imports.splitlines() if line.strip())
        elif isinstance(imports, list):
            import_lines = tuple(str(item).strip() for item in imports if str(item).strip())
        else:
            raise PatchError("new_imports must be a list of strings or a string")
        edits: list[tuple[int, str, str]] = []
        for item in data.get("line_edits") or []:
            if not isinstance(item, dict) or not str(item.get("line", "")).strip().isdigit():
                raise PatchError('line_edits items must be {"line": N, "new": "<the full new line>"}')
            edits.append((int(str(item["line"]).strip()), "", str(item.get("new", ""))))
        return ModelEdit(
            function_source=str(data["function_source"]), new_imports=import_lines, line_edits=tuple(edits)
        )

    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if blocks:
        function_source = blocks[0].strip("\n")
        import_lines: tuple[str, ...] = ()
        if len(blocks) > 1:
            import_lines = tuple(
                line.strip() for line in blocks[1].splitlines() if line.strip()
            )
        return ModelEdit(function_source=function_source, new_imports=import_lines)

    raise PatchError(
        "model reply must be JSON with function_source and new_imports, "
        "or fenced python blocks for the function (and optional imports)"
    )


def validate_function_source(function_source: str, *, expected_name: str) -> str:
    """Require a single top-level function with ``expected_name``; return text + newline.

    A method comes back indented as it sits in its class; it is dedented here
    and re-indented to the original def when spliced. An empty
    ``expected_name`` is a module-level statement: any statements that parse,
    or nothing to delete it.
    """
    text = textwrap.dedent(function_source.strip("\n")) + "\n"
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PatchError(f"rewritten function does not parse: {exc}") from exc
    if not expected_name:
        # An empty rewrite deletes the statement (an import nothing needs).
        return text if tree.body else ""
    defs = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(defs) != 1 or len(tree.body) != 1:
        raise PatchError(
            "function_source must contain exactly one top-level function definition"
        )
    if defs[0].name != expected_name:
        raise PatchError(
            f"function_source defines {defs[0].name!r}, expected {expected_name!r}"
        )
    return text


def resolve_line_edits(pack: ContextPack, edit: ModelEdit) -> ModelEdit:
    """Pin each line edit to the text of the line it replaces.

    Only a line listed in ``removed_name_uses`` outside the rewritten function
    may change, one line for one line."""
    if not edit.line_edits:
        return edit
    allowed = {item["line"] for item in pack.removed_name_uses if not item.get("in_this_function")}
    lines = pack.full_source.splitlines()
    resolved: list[tuple[int, str, str]] = []
    for line, old, new in edit.line_edits:
        if old:
            resolved.append((line, old, new))
            continue
        if line not in allowed or not 0 < line <= len(lines):
            raise PatchError(
                f"line_edits may only change lines listed in removed_name_uses outside this function (got line {line})"
            )
        if "\n" in new.strip("\n") or not new.strip():
            raise PatchError(f"line_edits line {line}: give one full replacement line")
        resolved.append((line, lines[line - 1], new.strip("\n")))
    return ModelEdit(edit.function_source, edit.new_imports, tuple(resolved))


def _apply_line_edits(lines: list[str], edits: tuple[tuple[int, str, str], ...], skip: tuple[int, int]) -> list[str]:
    """Replace each pinned line (outside ``skip``, 0-based [start, end)), nearest
    its recorded number. A line no longer there was rewritten since: left alone."""
    out = list(lines)
    for line, old, new in edits:
        hits = [
            index for index, text in enumerate(out)
            if not skip[0] <= index < skip[1] and text.rstrip("\r\n") == old.rstrip("\r\n")
        ]
        if not hits:
            continue
        index = min(hits, key=lambda at: abs(at + 1 - line))
        ending = out[index][len(out[index].rstrip("\r\n")):] or "\n"
        out[index] = _leading_ws(old) + new.strip() + ending
    return out


def strip_kept_decorators(function_source: str, kept: list[str]) -> str:
    """Drop decorators the model repeated: the def's own stay in the file above it."""
    if not kept:
        return function_source
    try:
        tree = ast.parse(function_source)
    except SyntaxError:
        return function_source
    node = next((item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if node is None or not node.decorator_list:
        return function_source
    remaining = list(kept)
    drop: set[int] = set()
    for deco in node.decorator_list:
        text = ast.unparse(deco)
        if text in remaining:
            remaining.remove(text)
            drop.update(range(deco.lineno - 1, (deco.end_lineno or deco.lineno)))
    if not drop:
        return function_source
    lines = function_source.splitlines(keepends=True)
    return "".join(line for index, line in enumerate(lines) if index not in drop)


def splice_edit(pack: ContextPack, edit: ModelEdit) -> tuple[str, str]:
    """Return ``(patched_source, unified_diff)`` built by Conduit (not the model)."""
    patched, diff, _span = splice_edit_with_span(pack, edit)
    return patched, diff


def splice_edit_with_span(
    pack: ContextPack, edit: ModelEdit
) -> tuple[str, str, tuple[int, int]]:
    """Like ``splice_edit`` plus the rewritten lines' 1-based span in the patch."""
    new_fn = validate_function_source(edit.function_source, expected_name=pack.function_name)
    if pack.function_name:
        new_fn = strip_kept_decorators(new_fn, pack.decorators)
    lines = pack.full_source.splitlines(keepends=True)
    start = pack.function_start_line - 1
    end = pack.function_end_line
    lines = _apply_line_edits(lines, edit.line_edits, (start, end))
    original_def = lines[start] if 0 <= start < len(lines) else ""
    indent = _leading_ws(original_def)
    fn_lines = _reindent_function_lines(new_fn, indent) if new_fn else []
    patched_lines = lines[:start] + fn_lines + lines[end:]
    patched = "".join(patched_lines)
    before = patched.count("\n")
    patched = add_imports(patched, edit.new_imports)
    shift = patched.count("\n") - before
    span = (start + 1 + shift, start + shift + max(len(fn_lines), 1))
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        raise PatchError(f"patched file does not parse: {exc}") from exc
    diff = unified_diff_for(pack.file_path, pack.full_source, patched)
    return patched, diff, span


def add_imports(source: str, new_imports: tuple[str, ...]) -> str:
    """Insert import lines after the existing import block; skip duplicates."""
    if not new_imports:
        return source
    existing = {_normalize_import(line) for line in _import_lines(source)}
    to_add: list[str] = []
    for line in new_imports:
        cleaned = line.strip()
        if not cleaned:
            continue
        if not cleaned.startswith(("import ", "from ")):
            raise PatchError(f"not an import line: {cleaned!r}")
        key = _normalize_import(cleaned)
        if key in existing:
            continue
        existing.add(key)
        to_add.append(cleaned)
    if not to_add:
        return source
    lines = source.splitlines(keepends=True)
    insert_at = _import_block_end(source)
    block = "".join(item + "\n" for item in to_add)
    return "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])


def unified_diff_for(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _reindent_function_lines(function_source: str, indent: str) -> list[str]:
    lines = function_source.splitlines()
    non_empty = [line for line in lines if line.strip()]
    common = min((len(line) - len(line.lstrip(" \t")) for line in non_empty), default=0)
    out: list[str] = []
    for line in lines:
        stripped = line[common:] if len(line) >= common else line.lstrip(" \t")
        if stripped.strip():
            out.append(indent + stripped + "\n")
        else:
            out.append("\n")
    return out or [indent + "pass\n"]


def _normalize_import(line: str) -> str:
    return " ".join(line.strip().split())


def _import_lines(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            start = node.lineno - 1
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            found.extend(lines[start:end])
    return found


def _import_block_end(source: str) -> int:
    """0-based line index after the last top-level import (or 0)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            continue
        else:
            break
    return end
