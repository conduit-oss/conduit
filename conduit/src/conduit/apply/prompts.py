"""Prompt text for propose / summarize steps.

No hard-coded fix recipes — the model sees only this site's evidence.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass

from conduit.apply.models import ContextPack
from conduit.apply.typecheck import _last_assignment


def propose_messages(pack: ContextPack, *, feedback: str = "") -> list[tuple[str, str]]:
    system = (
        "You fix one broken call site in a Python function.\n"
        "Rules:\n"
        "- Rewrite the flagged function body (same function name).\n"
        "- You may also rewrite helpers listed in editable_helpers. Those are same-file\n"
        "  functions that build an argument of the flagged call and are not used elsewhere\n"
        "  in the file. Return them in helper_sources, including decorators. Omit\n"
        "  helper_sources when they can stay unchanged. Do not edit any other function.\n"
        "- You may list new import lines if required.\n"
        "- Use only the release-diff evidence, new signature/docstring, and candidates "
        "provided (candidates are newer-wheel public symbols that share name tokens).\n"
        "- Respond with a JSON object only:\n"
        '  {"function_source": "<full def ...>", "new_imports": ["import ...", ...],\n'
        '   "helper_sources": {"<helper name>": "<full def ...>"}}\n'
        "- function_source must be the complete function definition (def/async def),\n"
        "  not a unified diff and not the whole file. Each helper source is the same.\n"
        "- If function_name is empty, the flagged line is a module-level statement:\n"
        "  function_source is that statement; return only its rewritten statement(s).\n"
        "- If an editable helper joins several turns into one string and the new parameter\n"
        "  is a sequence of records, rewrite that helper so each turn becomes its own record.\n"
        "  Do not put the joined string in one record. A turn whose role is not one of the\n"
        "  record's role values belongs in another parameter that accepts that text.\n"
        "- If feedback says a value has the wrong type, lacks an attribute, or wraps a joined\n"
        "  multi-turn string into one record, build the shape the new signature describes.\n"
        "  Editable helpers may change their return shape. Every other helper keeps its\n"
        "  current return type; rebuild the value inside this function from its own inputs.\n"
    )
    user = {
        "file": pack.file_path,
        "flagged_line": pack.site.line,
        "flagged_expr": pack.site.expr,
        "function_name": pack.function_name,
        "function_start_line": pack.function_start_line,
        "function_end_line": pack.function_end_line,
        "imports": pack.imports,
        "function_source": pack.function_source,
        "release_diff": pack.release_diff,
        "new_signature": pack.new_signature,
        "new_docstring": pack.new_docstring,
        "candidates": pack.candidates,
        "call_kwargs": pack.release_diff.get("call_kwargs") or [],
    }
    editable = editable_helpers(pack)
    if editable:
        user["editable_helpers"] = [{"name": item.name, "source": item.source} for item in editable]
    helpers = same_file_helpers(pack, exclude={item.name for item in editable})
    if helpers:
        # Read-only: shows how this function's inputs are shaped.
        user["helpers_called_read_only"] = helpers
    body = (
        "Context pack (JSON):\n"
        f"{json.dumps(user, indent=2)}\n\n"
        "Return JSON with function_source, new_imports, and helper_sources "
        "(only when an editable helper must change) that fixes the flagged call site."
    )
    if feedback:
        body += f"\n\nPrevious attempt failed:\n{feedback}\nRetry with a corrected rewrite."
    return [("system", system), ("user", body)]


@dataclass(frozen=True)
class EditableHelper:
    """A same-file helper the model may rewrite along with the flagged function."""

    name: str
    source: str
    start_line: int
    end_line: int
    def_line: int


def editable_helpers(pack: ContextPack, *, limit: int = 3, max_lines: int = 40) -> list[EditableHelper]:
    """Helpers that build an argument of the flagged call and are unused elsewhere.

    A helper used by another function in the file stays read-only: changing its
    return shape would break that caller, and the flagged function has to rebuild
    the value from its own inputs instead.
    """
    try:
        tree = ast.parse(pack.full_source)
    except (SyntaxError, ValueError):
        return []
    func = _flagged_function(tree, pack)
    call = _smallest_call(tree if func is None else func, pack.site.line)
    if call is None:
        return []
    produced = _producer_names(call, func)
    if not produced:
        return []
    lines = pack.full_source.splitlines()
    span = (pack.function_start_line, pack.function_end_line)
    out: list[EditableHelper] = []
    for node in _functions_in_order(tree):
        if node.name not in produced or node.name == pack.function_name:
            continue
        if span[0] <= node.lineno <= span[1]:
            continue
        if (node.end_lineno or node.lineno) - node.lineno >= max_lines:
            continue
        if not _exclusive_to_flagged(tree, node.name, pack.function_name, span):
            continue
        start = min([node.lineno, *[deco.lineno for deco in node.decorator_list]])
        end = node.end_lineno or node.lineno
        out.append(
            EditableHelper(
                name=node.name,
                source="\n".join(lines[start - 1 : end]),
                start_line=start,
                end_line=end,
                def_line=node.lineno,
            )
        )
        if len(out) >= limit:
            break
    return out


def same_file_helpers(
    pack: ContextPack, *, limit: int = 3, max_lines: int = 40, exclude: set[str] | None = None
) -> list[str]:
    """Sources of functions in the same file that the flagged function calls."""
    try:
        tree = ast.parse(pack.full_source)
    except (SyntaxError, ValueError):
        return []
    skipped = exclude or set()
    called: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and pack.function_start_line <= node.lineno <= pack.function_end_line
        ):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name and name != pack.function_name and name not in called and name not in skipped:
                called.append(name)
    lines = pack.full_source.splitlines()
    out: list[str] = []
    for node in _functions_in_order(tree):
        if (
            node.name in called
            and not (pack.function_start_line <= node.lineno <= pack.function_end_line)
            and (node.end_lineno or node.lineno) - node.lineno < max_lines
        ):
            start = min([node.lineno, *[deco.lineno for deco in node.decorator_list]])
            out.append("\n".join(lines[start - 1 : node.end_lineno]))
            if len(out) >= limit:
                break
    return out


def _flagged_function(tree: ast.AST, pack: ContextPack) -> ast.AST | None:
    if not pack.function_name:
        return None
    for node in _functions_in_order(tree):
        if node.name == pack.function_name and node.lineno == pack.function_start_line:
            return node
    return None


def _functions_in_order(tree: ast.AST) -> list[ast.AST]:
    found: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(child)
            visit(child)

    visit(tree)
    return found


def _smallest_call(tree: ast.AST, line: int) -> ast.Call | None:
    best: ast.Call | None = None
    best_span: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end:
            span = end - node.lineno
            if best is None or span < (best_span or 10**9):
                best = node
                best_span = span
    return best


def _producer_names(call: ast.Call, func: ast.AST | None) -> set[str]:
    """Names of calls whose result flows into an argument of ``call``."""
    found: set[str] = set()

    def walk(expr: ast.AST | None, depth: int) -> None:
        if expr is None or depth > 8:
            return
        if isinstance(expr, ast.Name):
            value = _last_assignment(func, expr.id, call.lineno) if func is not None else None
            if isinstance(value, ast.AnnAssign):
                value = value.value
            if isinstance(value, ast.expr):
                walk(value, depth + 1)
            return
        if isinstance(expr, ast.Call):
            name = expr.func.attr if isinstance(expr.func, ast.Attribute) else getattr(expr.func, "id", "")
            if name:
                found.add(name)
            for arg in (*expr.args, *(keyword.value for keyword in expr.keywords)):
                walk(arg, depth + 1)
            return
        for child in ast.iter_child_nodes(expr):
            if isinstance(child, ast.expr):
                walk(child, depth + 1)

    for arg in (*call.args, *(keyword.value for keyword in call.keywords)):
        walk(arg, 0)
    return found


def _exclusive_to_flagged(tree: ast.AST, name: str, flagged: str, span: tuple[int, int]) -> bool:
    """True when every call of ``name`` belongs to the flagged function (or the module statement)."""
    owners: set[str | None] = set()
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        callee = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
        if callee != name:
            continue
        owner = _owner_name(tree, call.lineno)
        if span[0] <= call.lineno <= span[1]:
            owner = flagged or None
        if owner == name:
            continue
        owners.add(owner)
    allowed = {flagged} if flagged else {None}
    return bool(owners) and owners <= allowed


def _owner_name(tree: ast.AST, line: int) -> str | None:
    best: ast.AST | None = None
    for node in _functions_in_order(tree):
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end and (best is None or node.lineno >= best.lineno):
            best = node
    return best.name if isinstance(best, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def summarize_messages(pack: ContextPack, diff: str) -> list[tuple[str, str]]:
    system = "Reply with one short sentence explaining why this change fixes the call site."
    user = (
        f"Change: {pack.site.change}\n"
        f"Expr: {pack.site.expr}\n"
        f"Diff:\n{diff}\n"
    )
    return [("system", system), ("user", user)]
