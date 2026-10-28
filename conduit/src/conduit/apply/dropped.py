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


def content_dropped(original: str, patched: str, line: int) -> list[str]:
    """String values the original call passed that do not reach the rewritten call.

    Only when the rewrite calls a different attribute, or passes a different set
    of keywords. A helper the new call hands the value to is followed when it
    returns a filtered or rebuilt literal; anything that cannot be followed is
    left alone, so a value that might still arrive is not reported. Empty when
    the two calls cannot be compared."""
    try:
        before, after = ast.parse(original), ast.parse(patched)
    except SyntaxError:
        return []
    old = _call_covering(before, line)
    if old is None:
        return []
    owner = _enclosing_name(before, line)
    new_func = _function_named(after, owner) if owner else None
    calls = [
        node
        for node in (ast.walk(new_func) if new_func is not None else ast.walk(after))
        if isinstance(node, ast.Call)
    ]
    new = _replacement_call(old, calls)
    if new is None or not _shape_changed(old, new):
        return []
    if any(kw.arg is None for kw in (*old.keywords, *new.keywords)) or any(
        isinstance(arg, ast.Starred) for arg in (*old.args, *new.args)
    ):
        return []
    origin = _string_values(old)
    if not origin:
        return []
    reaching: set[str] = set()
    for arg in _args_of(new):
        found = _reaching(arg, new_func, after)
        if found is None:
            return []
        reaching |= found
    return sorted(origin - reaching)


def _call_covering(tree: ast.AST, line: int) -> ast.Call | None:
    best: ast.Call | None = None
    span: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        start, end = node.lineno, node.end_lineno or node.lineno
        if start <= line <= end and (span is None or end - start < span):
            best, span = node, end - start
    return best


def _enclosing_name(tree: ast.AST, line: int) -> str:
    best: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or node.lineno >= best.lineno:
                best = node
    return best.name if isinstance(best, (ast.FunctionDef, ast.AsyncFunctionDef)) else ""


def _function_named(tree: ast.AST, name: str) -> ast.AST | None:
    if not name:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _callee_attr(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _shape_changed(old: ast.Call, new: ast.Call) -> bool:
    if _callee_attr(old) != _callee_attr(new):
        return True
    return {kw.arg for kw in old.keywords} != {kw.arg for kw in new.keywords}


def _replacement_call(old: ast.Call, calls: list[ast.Call]) -> ast.Call | None:
    """The rewritten call: an attribute call, not a helper wrapped around the value."""
    pool = [node for node in calls if isinstance(node.func, ast.Attribute)] or calls
    best: ast.Call | None = None
    best_score = -1
    for node in pool:
        score = _same_call(old, node)
        if _callee_attr(node) != _callee_attr(old):
            score += 1
        if score > best_score:
            best, best_score = node, score
    return best


def _string_values(node: ast.AST) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value
    }


def _reaching(expr: ast.AST, func: ast.AST | None, module: ast.AST) -> set[str] | None:
    """String values ``expr`` can pass on, or ``None`` when that cannot be known."""
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) and expr.value else set()
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return _merge(expr.elts, func, module)
    if isinstance(expr, ast.Dict):
        values = [value for value in expr.values if value is not None]
        return _merge([*expr.keys, *values], func, module)
    if isinstance(expr, ast.Name) and func is not None:
        bound = _bound_value(func, expr.id, getattr(expr, "lineno", 0) or 0)
        if bound is None:
            return None
        return _reaching(bound, func, module)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and len(expr.args) == 1:
        helper = _function_named(module, expr.func.id)
        if helper is None:
            return None
        return _filtered(helper, expr.args[0], func, module)
    return None


def _merge(nodes: list[ast.AST], func: ast.AST | None, module: ast.AST) -> set[str] | None:
    out: set[str] = set()
    for node in nodes:
        if node is None:
            continue
        found = _reaching(node, func, module)
        if found is None:
            return None
        out |= found
    return out


def _bound_value(func: ast.AST, name: str, line: int) -> ast.AST | None:
    best: tuple[int, ast.AST] | None = None
    for node in ast.walk(func):
        lineno = getattr(node, "lineno", 0) or 0
        if lineno > line:
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == name and (best is None or lineno >= best[0]):
                best = (lineno, node.value)
    return best[1] if best else None


def _filtered(helper: ast.AST, arg: ast.AST, func: ast.AST | None, module: ast.AST) -> set[str] | None:
    """Strings that survive ``helper(arg)`` when ``helper`` is a list filter."""
    returns = [node for node in ast.walk(helper) if isinstance(node, ast.Return) and node.value is not None]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.ListComp):
        return None
    comp = returns[0].value
    if len(comp.generators) != 1 or len(comp.generators[0].ifs) != 1:
        return None
    gen = comp.generators[0]
    if not isinstance(gen.target, ast.Name) or not isinstance(gen.iter, ast.Name):
        return None
    params = [item.arg for item in (*helper.args.args, *helper.args.posonlyargs)]
    if gen.iter.id not in params:
        return None
    elements = _list_elements(arg, func)
    if elements is None:
        return None
    kept: set[str] = set()
    for element in elements:
        decision = _keeps(element, gen.ifs[0], gen.target.id)
        if decision is None:
            return None
        if decision:
            kept |= _element_strings(element)
    return kept


def _list_elements(expr: ast.AST, func: ast.AST | None) -> list[ast.AST] | None:
    if isinstance(expr, ast.Name) and func is not None:
        bound = _bound_value(func, expr.id, getattr(expr, "lineno", 0) or 0)
        if bound is None:
            return None
        expr = bound
    if isinstance(expr, (ast.List, ast.Tuple)):
        return list(expr.elts)
    return None


def _keeps(element: ast.AST, test: ast.AST, target: str) -> bool | None:
    if not isinstance(element, ast.Dict) or not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    fields = _dict_fields(element)
    if fields is None:
        return None
    op = test.ops[0]
    left, right = test.left, test.comparators[0]
    key = _field_name(left, target)
    if key is None or not isinstance(right, ast.Constant):
        return None
    have = fields.get(key)
    if not isinstance(have, ast.Constant):
        return None
    if isinstance(op, ast.NotEq):
        return have.value != right.value
    if isinstance(op, ast.Eq):
        return have.value == right.value
    return None


def _field_name(expr: ast.AST, target: str) -> str | None:
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name) and expr.value.id == target:
        return expr.slice.value if isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str) else None
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and isinstance(expr.func.value, ast.Name)
        and expr.func.value.id == target
        and expr.func.attr == "get"
        and expr.args
        and isinstance(expr.args[0], ast.Constant)
        and isinstance(expr.args[0].value, str)
    ):
        return expr.args[0].value
    return None


def _dict_fields(node: ast.Dict) -> dict[str, ast.AST] | None:
    out: dict[str, ast.AST] = {}
    for key, value in zip(node.keys, node.values):
        if value is None or not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        out[key.value] = value
    return out


def _element_strings(node: ast.AST) -> set[str]:
    return _string_values(node)


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
