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
    """Values the original call carried that do not reach the rewritten call.

    Each argument is traced back through local assignments and helpers in this
    repo: f-strings, joins, and loops over a collection. On the rewritten call,
    a helper that skips items (``continue`` or a comprehension ``if``) drops
    those items unless another argument of the new call still carries them.
    Only when the rewrite calls a different attribute, or passes a different
    set of keywords. Anything that cannot be followed is left alone, so a value
    that might still arrive is not reported."""
    try:
        before, after = ast.parse(original), ast.parse(patched)
    except SyntaxError:
        return []
    old = _call_covering(before, line)
    if old is None:
        return []
    owner = _enclosing_name(before, line)
    old_func = _function_named(before, owner) if owner else None
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
    traced = _traced_drop(old, new, old_func, new_func, before, after)
    if traced is not None:
        return traced
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


class _Bag:
    """Strings a value carries, and the rows of a collection when they are known."""

    def __init__(self, texts: set[str] | None = None, rows: list[dict[str, str]] | None = None, fields: dict[str, str] | None = None) -> None:
        self.texts = set(texts or ())
        self.rows = rows
        self.fields = fields


class _Jump(Exception):
    def __init__(self, kind: str, value: _Bag | None = None) -> None:
        self.kind = kind
        self.value = value


def _traced_drop(old: ast.Call, new: ast.Call, old_func: ast.AST | None, new_func: ast.AST | None, before: ast.AST, after: ast.AST) -> list[str] | None:
    """``None`` when the original argument cannot be traced (the literal path is used)."""
    carried: set[str] = set()
    for arg in _args_of(old):
        bag = _eval(arg, old_func, before, 0)
        if bag is None:
            return None
        carried |= bag.texts
    if not carried:
        return []
    arriving: set[str] = set()
    for arg in _args_of(new):
        bag = _eval(arg, new_func, after, 0)
        if bag is None:
            return []
        arriving |= bag.texts
    return sorted(carried - arriving)


def _eval(expr: ast.AST, func: ast.AST | None, module: ast.AST, depth: int) -> _Bag | None:
    if depth > 6:
        return None
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, str) and expr.value.strip():
            return _Bag({expr.value})
        return _Bag()
    if isinstance(expr, ast.JoinedStr):
        texts: set[str] = set()
        for part in expr.values:
            if isinstance(part, ast.FormattedValue):
                bag = _eval(part.value, func, module, depth + 1)
                if bag is None:
                    return None
                texts |= bag.texts
        return _Bag(texts)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left, right = _eval(expr.left, func, module, depth + 1), _eval(expr.right, func, module, depth + 1)
        if left is None or right is None:
            return None
        return _Bag(left.texts | right.texts)
    if isinstance(expr, (ast.List, ast.Tuple)):
        return _collection(list(expr.elts), func, module, depth)
    if isinstance(expr, ast.Dict):
        fields = _const_fields(expr)
        if fields is None:
            return None
        return _Bag(set(fields.values()), fields=fields)
    if isinstance(expr, ast.Subscript):
        bag = _eval(expr.value, func, module, depth + 1)
        key = expr.slice.value if isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str) else None
        if bag is None or key is None or bag.fields is None or key not in bag.fields:
            return None
        return _Bag({bag.fields[key]})
    if isinstance(expr, ast.Name):
        if func is None:
            return None
        bound = _bound_value(func, expr.id, getattr(expr, "lineno", 0) or 0)
        if bound is None:
            return None
        return _eval(bound, func, module, depth + 1)
    if isinstance(expr, ast.ListComp):
        return _comprehension(expr, func, module, depth)
    if isinstance(expr, ast.Call):
        return _call_value(expr, func, module, depth)
    return None


def _collection(elts: list[ast.AST], func: ast.AST | None, module: ast.AST, depth: int) -> _Bag | None:
    rows: list[dict[str, str]] = []
    texts: set[str] = set()
    for elt in elts:
        if isinstance(elt, ast.Dict):
            fields = _const_fields(elt)
            if fields is None:
                return None
            rows.append(fields)
            texts |= set(fields.values())
            continue
        bag = _eval(elt, func, module, depth + 1)
        if bag is None:
            return None
        texts |= bag.texts
    return _Bag(texts, rows=rows)


def _const_fields(node: ast.Dict) -> dict[str, str] | None:
    out: dict[str, str] = {}
    for key, value in zip(node.keys, node.values):
        if value is None or not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return None
        out[key.value] = value.value
    return out


def _call_value(expr: ast.Call, func: ast.AST | None, module: ast.AST, depth: int) -> _Bag | None:
    if isinstance(expr.func, ast.Attribute) and expr.func.attr == "join" and isinstance(expr.func.value, ast.Constant):
        if len(expr.args) != 1:
            return None
        return _eval(expr.args[0], func, module, depth + 1)
    if isinstance(expr.func, ast.Attribute) and expr.func.attr == "get" and expr.args:
        bag = _eval(expr.func.value, func, module, depth + 1)
        key = expr.args[0].value if isinstance(expr.args[0], ast.Constant) else None
        if bag is None or not isinstance(key, str) or bag.fields is None or key not in bag.fields:
            return None
        return _Bag({bag.fields[key]})
    if not isinstance(expr.func, ast.Name):
        return None
    helper = _function_named(module, expr.func.id)
    if helper is None or depth > 4:
        return None
    params = [arg.arg for arg in (*helper.args.posonlyargs, *helper.args.args) if arg.arg not in {"self", "cls"}]
    if len(params) != len(expr.args) or expr.keywords:
        return None
    scope: dict[str, _Bag] = {}
    for name, arg in zip(params, expr.args):
        bag = _eval(arg, func, module, depth + 1)
        if bag is None:
            return None
        scope[name] = bag
    return _exec(helper, scope, module, depth + 1)


def _comprehension(expr: ast.ListComp, func: ast.AST | None, module: ast.AST, depth: int, scope: dict[str, _Bag] | None = None) -> _Bag | None:
    if len(expr.generators) != 1 or len(expr.generators[0].ifs) > 1:
        return None
    gen = expr.generators[0]
    if not isinstance(gen.target, ast.Name):
        return None
    if scope is not None and isinstance(gen.iter, ast.Name) and gen.iter.id in scope:
        source = scope[gen.iter.id]
    else:
        source = _eval(gen.iter, func, module, depth + 1)
    if source is None or source.rows is None:
        return None
    kept: list[dict[str, str]] = []
    texts: set[str] = set()
    for row in source.rows:
        scope = {gen.target.id: _Bag(set(row.values()), fields=row)}
        if gen.ifs:
            decision = _decide(gen.ifs[0], scope)
            if decision is None:
                return None
            if not decision:
                continue
        kept.append(row)
        texts |= set(row.values())
    return _Bag(texts, rows=kept)


def _exec(func: ast.AST, scope: dict[str, _Bag], module: ast.AST, depth: int) -> _Bag | None:
    try:
        for stmt in func.body:
            jumped = _exec_stmt(stmt, scope, module, depth)
            if isinstance(jumped, _Bag):
                return jumped
    except _Jump as jump:
        if jump.kind == "return":
            return jump.value
        return None
    return _Bag()


def _exec_stmt(stmt: ast.stmt, scope: dict[str, _Bag], module: ast.AST, depth: int) -> _Bag | None:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        bag = _eval_in(stmt.value, scope, module, depth)
        if bag is None:
            raise _Jump("unknown")
        scope[stmt.targets[0].id] = bag
        return None
    if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name) and isinstance(stmt.op, ast.Add):
        bag = _eval_in(stmt.value, scope, module, depth)
        have = scope.get(stmt.target.id)
        if bag is None or have is None:
            raise _Jump("unknown")
        have.texts |= bag.texts
        return None
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return _Bag()
        bag = _eval_in(stmt.value, scope, module, depth)
        if bag is None:
            raise _Jump("unknown")
        return bag
    if isinstance(stmt, ast.For) and isinstance(stmt.target, ast.Name):
        source = _eval_in(stmt.iter, scope, module, depth)
        if source is None or source.rows is None:
            raise _Jump("unknown")
        for row in source.rows:
            scope[stmt.target.id] = _Bag(set(row.values()), fields=row)
            try:
                for inner in stmt.body:
                    _exec_stmt(inner, scope, module, depth)
            except _Jump as jump:
                if jump.kind == "continue":
                    continue
                raise
        return None
    if isinstance(stmt, ast.If):
        decision = _decide(stmt.test, scope)
        if decision is None:
            raise _Jump("unknown")
        body = stmt.body if decision else stmt.orelse
        for inner in body:
            found = _exec_stmt(inner, scope, module, depth)
            if isinstance(found, _Bag):
                return found
        return None
    if isinstance(stmt, ast.Continue):
        raise _Jump("continue")
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "append"
            and isinstance(call.func.value, ast.Name)
            and len(call.args) == 1
        ):
            target = scope.get(call.func.value.id)
            bag = _eval_in(call.args[0], scope, module, depth)
            if target is None or bag is None:
                raise _Jump("unknown")
            target.texts |= bag.texts
            if target.rows is not None and bag.fields is not None:
                target.rows.append(dict(bag.fields))
            return None
    if isinstance(stmt, ast.Pass):
        return None
    raise _Jump("unknown")


def _eval_in(expr: ast.AST, scope: dict[str, _Bag], module: ast.AST, depth: int) -> _Bag | None:
    if isinstance(expr, ast.Name) and expr.id in scope:
        return scope[expr.id]
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name) and expr.value.id in scope:
        bag = scope[expr.value.id]
        key = expr.slice.value if isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str) else None
        if key is None or bag.fields is None or key not in bag.fields:
            return None
        return _Bag({bag.fields[key]})
    if isinstance(expr, ast.JoinedStr):
        texts: set[str] = set()
        for part in expr.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            bag = _eval_in(part.value, scope, module, depth + 1)
            if bag is None:
                return None
            texts |= bag.texts
        return _Bag(texts)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "join":
        if len(expr.args) != 1:
            return None
        return _eval_in(expr.args[0], scope, module, depth + 1)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left, right = _eval_in(expr.left, scope, module, depth + 1), _eval_in(expr.right, scope, module, depth + 1)
        if left is None or right is None:
            return None
        return _Bag(left.texts | right.texts)
    if isinstance(expr, ast.ListComp):
        return _comprehension(expr, None, module, depth, scope)
    if isinstance(expr, (ast.List, ast.Tuple, ast.Dict, ast.Constant)):
        return _eval(expr, None, module, depth + 1)
    if isinstance(expr, ast.Name):
        return None
    return _eval(expr, None, module, depth + 1)


def _decide(test: ast.AST, scope: dict[str, _Bag]) -> bool | None:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    left, right = test.left, test.comparators[0]
    key = None
    bag = None
    if isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name):
        bag = scope.get(left.value.id)
        if isinstance(left.slice, ast.Constant) and isinstance(left.slice.value, str):
            key = left.slice.value
    if bag is None or key is None or bag.fields is None or not isinstance(right, ast.Constant):
        return None
    have = bag.fields.get(key)
    if have is None:
        return None
    if isinstance(test.ops[0], ast.Eq):
        return have == right.value
    if isinstance(test.ops[0], ast.NotEq):
        return have != right.value
    return None


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
