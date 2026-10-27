"""Arguments a rewrite stops passing, and the human sentence about them.

A rewritten call can quietly lose an argument the original passed (a keyword
the new release no longer takes, or a positional that went away). Tests and
rechecks can still pass, so the report says so in one sentence. Everything is
compared from the two sources and the new release's own code: the original
call's arguments against the rewritten call's, nothing about any library.
"""

from __future__ import annotations

import ast
from contextvars import ContextVar
from typing import Any

#: Set around the per-site check; ``_type_breaks`` fills ``{"names": [...], "note": str}``.
DROPPED: ContextVar[dict[str, Any] | None] = ContextVar("conduit_dropped", default=None)


def _dump(node: ast.AST) -> str:
    return ast.dump(node)


def _function_nodes(tree: ast.Module, span: tuple[int, int]) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and span[0] <= node.lineno <= span[1]
    ]


def _loaded(nodes: list[ast.AST]) -> set[str]:
    """Every expression the rewritten code reads (stores are not reads)."""
    seen: set[str] = set()
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.expr) and not isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
                seen.add(_dump(node))
    return seen


def _strings(nodes: list[ast.AST]) -> set[str]:
    return {
        node.value
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _args_of(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(kw.value for kw in call.keywords)]


def _inside(call: ast.Call) -> set[str]:
    return {_dump(node) for arg in _args_of(call) for node in ast.walk(arg) if isinstance(node, ast.expr)}


def _same_call(old: ast.Call, new: ast.Call) -> int:
    """How many arguments (by name or value) the two calls share."""
    values = {_dump(arg) for arg in _args_of(old)}
    names = {kw.arg for kw in old.keywords if kw.arg}
    return len(values & {_dump(arg) for arg in _args_of(new)}) + len(names & {kw.arg for kw in new.keywords if kw.arg})


def positional_names(call: ast.Call, order: list[str]) -> list[str]:
    return [order[i] if i < len(order) else f"positional argument {i + 1}" for i in range(len(call.args))]


def dropped_from(
    old: ast.Call,
    tree: ast.Module,
    span: tuple[int, int],
    order: list[str] | None = None,
) -> tuple[list[str], ast.Call | None]:
    """``(names, rewritten call)``: the original call's arguments the rewrite no longer passes.

    The rewritten call is the call in ``span`` that shares the most arguments with
    the original one. An argument counts as kept when its name is still passed,
    its value expression is still passed (under any name, or by position), or a
    value that is not a constant is still read anywhere in the rewritten
    function. A call with ``*`` / ``**`` is not judged: it may pass anything."""
    if any(kw.arg is None for kw in old.keywords) or any(isinstance(arg, ast.Starred) for arg in old.args):
        return [], None
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and span[0] <= node.lineno <= span[1]
    ]
    best: ast.Call | None = None
    best_score = 0
    for node in calls:
        score = _same_call(old, node)
        if score > best_score:
            best, best_score = node, score
    if best is None or any(kw.arg is None for kw in best.keywords) or any(isinstance(arg, ast.Starred) for arg in best.args):
        return [], None
    functions = _function_nodes(tree, span) or [tree]
    reads, words, inside = _loaded(functions), _strings(functions), _inside(best)
    passed = {kw.arg for kw in best.keywords}
    labels = [(kw.arg or "", kw.value) for kw in old.keywords]
    labels += list(zip(positional_names(old, order or []), old.args))
    dropped: list[str] = []
    for name, value in labels:
        if name in passed or name in words:
            continue
        key = _dump(value)
        constant = isinstance(value, ast.Constant)
        if key in inside or (not constant and key in reads):
            continue
        dropped.append(name)
    return dropped, best


def note_for(names: list[str], rejected: list[str], owner: str) -> str:
    """One plain sentence. ``rejected`` are names the new release's callee does not accept."""
    if not names:
        return ""

    def quoted(items: list[str]) -> str:
        return ", ".join(f"`{item}`" if " " not in item else item for item in items)

    refused = [name for name in names if name in rejected]
    other = [name for name in names if name not in rejected]
    parts: list[str] = []
    if refused:
        callee = f"`{owner}`" if owner else "the call"
        plural = len(refused) > 1
        parts.append(
            f"Dropped {quoted(refused)}: the new release's {callee} doesn't accept "
            f"{'them' if plural else 'it'}, so {'these settings are' if plural else 'this setting is'} now ignored."
        )
    if other:
        parts.append(f"Dropped {quoted(other)}: removed without a matching parameter in the new call.")
    return " ".join(parts)


def import_swap_why(original: str, patched: str, line: int, change: str = "") -> str:
    """The reason for an import line that changed, built from the two sources.

    Empty unless the original line is an import statement the rewrite replaced."""
    try:
        before, after = ast.parse(original), ast.parse(patched)
    except (SyntaxError, ValueError):
        return ""
    imports = (ast.Import, ast.ImportFrom)
    old = [n for n in ast.walk(before) if isinstance(n, imports) and n.lineno <= line <= (n.end_lineno or n.lineno)]
    if not old:
        return ""
    old_text = {ast.unparse(n) for n in ast.walk(before) if isinstance(n, imports)}
    new_text = [ast.unparse(n) for n in after.body if isinstance(n, imports)] + [
        ast.unparse(n) for n in ast.walk(after) if isinstance(n, imports) and n not in after.body
    ]
    gone = ast.unparse(old[0])
    if gone in new_text:
        return ""
    added = [text for text in dict.fromkeys(new_text) if text not in old_text]
    if not added:
        return f"Removed the import `{gone}`" + (f" ({change})" if change else "") + "."
    shown = " and ".join(f"`{text}`" for text in added[:3])
    return f"Replaced the import `{gone}` with {shown}" + (f" ({change})" if change else "") + "."
