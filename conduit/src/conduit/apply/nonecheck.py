"""Values the new release types as Optional, used where None does not work.

A field is Optional when its annotation admits None (``X | None``,
``Optional[X]``, ``Union[..., None]``) or its default is None, read from the
new release's own class bodies. A read of such a field is checked where it
goes: into a repo function or class whose parameter does not take None (its
annotation, or arithmetic / comparison / attribute use in its body), into
arithmetic, augmented assignment or a comparison here, or out as the return
value of a function annotated with a non-Optional type. ``x or default``,
``x if x is not None else ...``, a test on it, and an ``is None`` check
anywhere in the function count as handled. No library, type or method
names are listed here; everything comes from the code being checked.
"""

from __future__ import annotations

import ast
from contextvars import ContextVar
from pathlib import Path

#: Set by the final recheck; the per-site check does not run this.
NONE_CHECK: ContextVar[bool] = ContextVar("conduit_none_check", default=False)

_ANY = {"Any", "object"}


def allows_none(annotation: ast.AST | None, depth: int = 0) -> bool:
    """The annotation admits None."""
    node = annotation
    if node is None or depth > 6:
        return False
    if isinstance(node, ast.Constant):
        if node.value is None:
            return True
        if isinstance(node.value, str):
            try:
                return allows_none(ast.parse(node.value.strip(), mode="eval").body, depth + 1)
            except SyntaxError:
                return False
        return False
    if isinstance(node, ast.Name):
        return node.id == "None"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return allows_none(node.left, depth + 1) or allows_none(node.right, depth + 1)
    if isinstance(node, ast.Subscript):
        head = node.value.attr if isinstance(node.value, ast.Attribute) else getattr(node.value, "id", "")
        parts = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if head == "Optional":
            return True
        if head == "Annotated":
            return allows_none(parts[0], depth + 1)
        if head == "Union":
            return any(allows_none(part, depth + 1) for part in parts)
    return False


def _is_any(annotation: ast.AST | None) -> bool:
    node = annotation
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip().rsplit(".", 1)[-1] in _ANY
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
    return name in _ANY


def field_decl(modules, module_name: str, klass: ast.ClassDef, attr: str, depth: int = 0):
    """``(annotation, default)`` of a declared field, through the release's base classes."""
    from conduit.apply.typecheck import _class_statements, _find_class, _type_name

    for stmt in _class_statements(klass.body):
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == attr:
            return stmt.annotation, stmt.value
    if depth > 4:
        return None
    for base in klass.bases:
        name = _type_name(base.value if isinstance(base, ast.Subscript) else base)
        hit = _find_class(modules, name, module_name) if name else None
        if hit is not None and hit[1] is not klass:
            found = field_decl(modules, hit[0], hit[1], attr, depth + 1)
            if found is not None:
                return found
    return None


def _none_default(value: ast.AST | None) -> bool:
    """``= None``, or a field factory call whose ``default`` is None."""
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    if isinstance(value, ast.Call):
        if value.args and isinstance(value.args[0], ast.Constant) and value.args[0].value is None:
            return True
        return any(
            kw.arg == "default" and isinstance(kw.value, ast.Constant) and kw.value.value is None
            for kw in value.keywords
        )
    return False


def optional_field(modules, typed, expr: ast.AST) -> str:
    """The Optional annotation text when ``expr`` (``a.b.field``) may be None in the release."""
    from conduit.apply.typecheck import _expr_classes

    if not isinstance(expr, ast.Attribute):
        return ""
    owners = _expr_classes(modules, typed, expr.value, 0)
    for module_name, klass in owners:
        found = field_decl(modules, module_name, klass, expr.attr)
        if found is None:
            continue
        annotation, default = found
        if allows_none(annotation):
            return ast.unparse(annotation)
        if _none_default(default):
            return f"{ast.unparse(annotation)} = None"
    return ""


def _parents(root: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(root) for child in ast.iter_child_nodes(parent)}


def _mentions(node: ast.AST, key: str) -> bool:
    return any(ast.unparse(sub) == key for sub in ast.walk(node) if isinstance(sub, (ast.Name, ast.Attribute)))


def _none_checked(scope: ast.AST, key: str) -> bool:
    """``key is None`` / ``key is not None`` somewhere in the function."""
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Compare)
            and ast.unparse(node.left) == key
            and all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops)
            and all(isinstance(item, ast.Constant) and item.value is None for item in node.comparators)
        ):
            return True
    return False


def guarded(node: ast.AST, key: str, parents: dict[ast.AST, ast.AST], scope: ast.AST) -> bool:
    """This read of ``key`` already handles None."""
    if _none_checked(scope, key):
        return True
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or) and parent.values[-1] is not child:
            return True  # ``key or default``
        if isinstance(parent, (ast.IfExp, ast.If, ast.While)) and child is not parent.test and _mentions(parent.test, key):
            return True  # ``... if key ... else ...`` / under ``if key:``
        if isinstance(parent, ast.stmt) and not isinstance(parent, (ast.If, ast.While)):
            # A statement inside a test-guarded block is still guarded by that block.
            pass
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        child, parent = parent, parents.get(parent)
    return False


def _direct_use(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """What this expression does with the value right where it is read."""
    parent = parents.get(node)
    if isinstance(parent, ast.BinOp):
        return "adds it up" if isinstance(parent.op, ast.Add) else "does arithmetic with it"
    if isinstance(parent, ast.AugAssign):
        return "adds it up" if isinstance(parent.op, ast.Add) else "does arithmetic with it"
    if isinstance(parent, ast.Compare):
        others = [parent.left, *parent.comparators]
        if all(isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) for op in parent.ops):
            return ""
        if any(isinstance(item, ast.Constant) and item.value is None for item in others):
            return ""
        return "compares it"
    if isinstance(parent, ast.Attribute) and parent.value is node:
        return f"reads `.{parent.attr}` on it"
    return ""


def _param_use(func: ast.AST, name: str) -> str:
    """How the callee uses a parameter in a way None does not survive."""
    parents = _parents(func)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            if guarded(node, name, parents, func):
                continue
            use = _direct_use(node, parents)
            if use:
                return use
    return ""


def _callee_param(target: ast.AST, call: ast.Call, value: ast.AST) -> tuple[ast.AST, ast.arg | None, ast.AST | None]:
    """The callee function and the parameter ``value`` binds to (with its default)."""
    func = target
    skip = 0
    if isinstance(target, ast.ClassDef):
        func = next(
            (stmt for stmt in target.body if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"), None
        )
        if func is None:
            return target, None, None
        skip = 1
    elif any(
        isinstance(deco, ast.Name) and deco.id in {"staticmethod"} for deco in getattr(target, "decorator_list", [])
    ):
        skip = 0
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for index, item in enumerate(call.args):
        if item is value and index + skip < len(positional):
            return func, positional[index + skip], defaults[index + skip]
    for kw in call.keywords:
        if kw.value is value and kw.arg:
            for arg, default in zip(positional, defaults):
                if arg.arg == kw.arg:
                    return func, arg, default
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if arg.arg == kw.arg:
                    return func, arg, default
    return func, None, None


def _repo_defs(root: Path, path: str, tree: ast.Module, trees) -> tuple[dict[str, ast.AST], dict[str, list[ast.AST]]]:
    """Top-level classes and functions: the ones this module sees, and all of them by name."""
    from conduit.apply.typecheck import _repo_trees

    defs: dict[str, list[ast.AST]] = {}
    by_file: dict[str, dict[str, ast.AST]] = {}
    for rel, module in _repo_trees(root, trees):
        for node in module.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append(node)
                by_file.setdefault(rel, {})[node.name] = node
    seen: dict[str, ast.AST] = {
        node.name: node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            base = path.rsplit("/", 1)[0].split("/") if "/" in path else []
            parts = (base[: len(base) - (node.level - 1)] if node.level > 1 else base) if node.level else []
            parts = parts + (node.module.split(".") if node.module else [])
            for alias in node.names:
                for rel in ("/".join(parts) + ".py", "/".join(parts) + "/__init__.py"):
                    hit = by_file.get(rel, {}).get(alias.name)
                    if hit is not None:
                        seen[alias.asname or alias.name] = hit
    return seen, defs


def _methods(defs: dict[str, list[ast.AST]], name: str) -> list[ast.AST]:
    return [
        stmt
        for nodes in defs.values()
        for node in nodes
        if isinstance(node, ast.ClassDef)
        for stmt in node.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name
    ]


def _call_sink(root: Path, path: str, tree: ast.Module, trees, call: ast.Call, value: ast.AST, cache: dict) -> str:
    if "defs" not in cache:
        cache["defs"] = _repo_defs(root, path, tree, trees)
    seen, defs = cache["defs"]
    if isinstance(call.func, ast.Name):
        found = [seen[call.func.id]] if call.func.id in seen else defs.get(call.func.id, [])
        label = call.func.id
    elif isinstance(call.func, ast.Attribute):
        found = _methods(defs, call.func.attr)
        label = call.func.attr
    else:
        return ""
    if len(found) != 1:
        return ""
    func, param, default = _callee_param(found[0], call, value)
    if param is None:
        return ""
    if allows_none(param.annotation) or _is_any(param.annotation) or (
        isinstance(default, ast.Constant) and default.value is None
    ):
        return ""
    use = _param_use(func, param.arg)
    if use:
        return f"`{label}` {use}"
    if param.annotation is not None:
        return f"`{label}` takes it as `{ast.unparse(param.annotation)}`"
    return ""


def none_breaks(modules, root: Path, trees, path: str, tree: ast.Module, func: ast.AST, typed, package: str, version: str) -> list[str]:
    """Findings for Optional release fields this function passes on, computes with, or returns unguarded."""
    parents = _parents(func)
    out: list[str] = []
    cache: dict = {}
    reads: list[tuple[ast.AST, str, str, str]] = []  # (node, key, field, annotation)
    aliases: dict[str, tuple[str, str]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            annotation = optional_field(modules, typed, node)
            if annotation:
                reads.append((node, ast.unparse(node), node.attr, annotation))
    for node, key, field, annotation in list(reads):
        parent = parents.get(node)
        if (
            isinstance(parent, ast.Assign) and parent.value is node and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
        ):
            aliases[parent.targets[0].id] = (field, annotation)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases:
            field, annotation = aliases[node.id]
            reads.append((node, node.id, field, annotation))
    for node, key, field, annotation in reads:
        if guarded(node, key, parents, func):
            continue
        parent = parents.get(node)
        sink = ""
        if isinstance(parent, ast.Call) and (node in parent.args or any(kw.value is node for kw in parent.keywords)):
            sink = _call_sink(root, path, tree, trees, parent, node, cache)
        elif isinstance(parent, ast.keyword):
            call = parents.get(parent)
            if isinstance(call, ast.Call):
                sink = _call_sink(root, path, tree, trees, call, node, cache)
        elif isinstance(parent, ast.Return):
            returns = getattr(func, "returns", None)
            if returns is not None and not allows_none(returns) and not _is_any(returns):
                sink = f"`{getattr(func, 'name', 'the function')}` returns it as `{ast.unparse(returns)}`"
        else:
            use = _direct_use(node, parents)
            if use:
                sink = f"line {node.lineno} {use}"
        if not sink:
            continue
        text = (
            f"{path}:{node.lineno} {key}: {field} may be None ({annotation}) in {package} {version}, but {sink}; "
            f"handle None where it is read (`{key} or 0`, `... if {key} is not None else ...`, or an is None "
            f"check), or pass it only where None is accepted"
        )
        if text not in out:
            out.append(text)
    return out
