"""Static checks of a rewrite against the new release's own types.

Both checks read types only from the new wheel's code (and, for a base class
the wheel imports from another installed distribution, that class itself).
Anything that cannot be resolved is treated as unknown and never flagged.
"""

from __future__ import annotations

import ast
from contextvars import ContextVar
from pathlib import Path

from conduit.apply.signatures import (
    _find_class,
    _is_overload,
    _type_name,
    _typed_dict_class,
    missing_member,
    resolve_callable,
)

#: Set by the apply engine around a recheck: collects the release values a
#: rewrite passes into repo functions (see ``_callee_reads``).
FLOWS: ContextVar[list[dict] | None] = ContextVar("conduit_flows", default=None)

# Python's own typing vocabulary (not any library's API).
_BUILTINS = {"str", "bytes", "int", "float", "bool", "list", "tuple", "dict", "set"}
_ITERABLES = {
    "Iterable", "Sequence", "Collection", "Iterator", "List", "list",
    "MutableSequence", "Tuple", "tuple", "Set", "set", "FrozenSet", "AbstractSet",
}
_MAPPINGS = {"Mapping", "MutableMapping", "Dict", "dict"}
_UNWRAP = {"Optional", "Required", "NotRequired", "Annotated", "ReadOnly", "Final"}
_ANY = {"Any", "object"}
_MAX_DEPTH = 6


# --- 1. argument kinds -----------------------------------------------------


def argument_breaks(
    modules: dict[str, ast.Module],
    tree: ast.Module,
    path: str,
    chain,
    package: str,
    version: str,
) -> list[str]:
    """Keyword arguments whose inferred builtin kind the new annotation cannot take."""
    found = resolve_callable(modules, chain.sdk_method)
    if found is None:
        missing = missing_member(modules, chain.sdk_method)
        if missing is None:
            return []
        owner, names = missing
        attr = chain.sdk_method.rsplit(".", 1)[-1]
        return [
            f"{path}:{chain.call_lineno} {chain.call_expr}: {owner} in {package} {version} has no "
            f"{attr} (its methods: {', '.join(names[:20])})"
        ]
    module_name, node = found
    call = _call_at(tree, chain.call_lineno, chain.call_expr)
    if call is None:
        return []
    func = _enclosing_function(tree, call.lineno)
    params = {arg.arg: arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    out: list[str] = []
    positional = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
    if positional and positional[0] in {"self", "cls"}:
        positional = positional[1:]
    if node.args.vararg is None and not any(isinstance(arg, ast.Starred) for arg in call.args):
        if len(call.args) > len(positional):
            keyword_only = [arg.arg for arg in node.args.kwonlyargs][:12]
            out.append(
                f"{path}:{call.lineno} {chain.call_expr}: passes {len(call.args)} positional arguments but "
                f"{_owner(chain.sdk_method)} in {package} {version} takes {len(positional)} "
                f"({', '.join(positional) or 'none'}); pass the rest by keyword ({', '.join(keyword_only)})"
            )
    for keyword in call.keywords:
        param = params.get(keyword.arg or "")
        if param is None or param.annotation is None:
            continue
        kind = infer_kind(keyword.value, tree, func, call.lineno)
        if kind is None:
            continue
        if accepts(modules, module_name, param.annotation, kind) is not False:
            continue
        expected = ast.unparse(param.annotation)
        text = (
            f"{path}:{call.lineno} {chain.call_expr}: {keyword.arg}= passes a {kind} but "
            f"{_owner(chain.sdk_method)} in {package} {version} expects {expected}"
        )
        shape = _shape(modules, module_name, param.annotation)
        if shape:
            text += f", i.e. {shape}"
        origin = _origin(keyword.value, func, call.lineno)
        if origin:
            text += f" ({origin})"
        fields = _typed_dict_fields(modules, module_name, param.annotation)
        if fields:
            text += " (" + "; ".join(fields) + ")"
        out.append(text)
    return out


def _shape(modules: dict[str, ast.Module], near: str, node: ast.AST | None, depth: int = 0) -> str:
    """A literal-looking sketch of a TypedDict-based annotation (required keys only)."""
    if node is None or depth > _MAX_DEPTH:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return ""
    if isinstance(node, ast.Subscript):
        base = _type_name(node.value)
        items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if base in _UNWRAP:
            return _shape(modules, near, items[0], depth + 1)
        if base in _ITERABLES:
            inner = _shape(modules, near, items[0], depth + 1)
            return f"a list like [{inner}, ...]" if inner.startswith("{") else ""
        return ""
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = _type_name(node)
        klass = _typed_dict_class(modules, name)
        if klass is not None:
            keys = []
            for stmt in klass.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    annotation = stmt.annotation
                    if isinstance(annotation, ast.Subscript) and _type_name(annotation.value) == "NotRequired":
                        continue
                    if isinstance(annotation, ast.Subscript) and _type_name(annotation.value) == "Required":
                        annotation = annotation.slice
                    keys.append(f'"{stmt.target.id}": <{ast.unparse(annotation)[:50]}>')
            return "{" + ", ".join(keys) + "}"
        alias = _alias_value(modules, near, name)
        if alias is not None:
            return _shape(modules, alias[0], alias[1], depth + 1)
    return ""


def _origin(expr: ast.AST, func: ast.AST | None, line: int) -> str:
    """Where a named value comes from, so the rewrite can build the right type instead."""
    if not isinstance(expr, ast.Name) or func is None:
        return ""
    for arg in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs):
        if arg.arg == expr.id and arg.annotation is not None:
            return f"{expr.id} is a parameter annotated {ast.unparse(arg.annotation)}"
    value = _last_assignment(func, expr.id, line)
    if isinstance(value, ast.expr):
        return f"{expr.id} = {ast.unparse(value)[:120]}"
    return ""


def accepts(
    modules: dict[str, ast.Module], near: str, node: ast.AST | None, kind: str, depth: int = 0
) -> bool | None:
    """True / False when the annotation decides it, None when unknown."""
    if node is None or depth > _MAX_DEPTH:
        return None
    if isinstance(node, ast.Constant):
        if node.value is None:
            return False
        if isinstance(node.value, str):
            try:
                return accepts(modules, near, ast.parse(node.value.strip(), mode="eval").body, kind, depth + 1)
            except SyntaxError:
                return None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _any_of([accepts(modules, near, side, kind, depth + 1) for side in (node.left, node.right)])
    if isinstance(node, ast.Subscript):
        base = _type_name(node.value)
        items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if base == "Union":
            return _any_of([accepts(modules, near, item, kind, depth + 1) for item in items])
        if base in _UNWRAP:
            return accepts(modules, near, items[0], kind, depth + 1)
        if base == "Literal":
            return any(type(item.value).__name__ == kind for item in items if isinstance(item, ast.Constant))
        if base in _ITERABLES:
            if kind in {"list", "tuple", "set"}:
                return True
            if kind == "str":
                # A str is an iterable of str: only an element type that takes str allows it.
                return accepts(modules, near, items[0], kind, depth + 1)
            return False
        if base in _MAPPINGS:
            return kind == "dict"
        return None
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = _type_name(node)
        if name in _ANY:
            return True
        if name == "None":
            return False
        if name in _BUILTINS:
            return kind == name or (name == "float" and kind == "int") or (name == "int" and kind == "bool")
        if name in _ITERABLES:
            return kind in {"list", "tuple", "set", "str"}
        if name in _MAPPINGS:
            return kind == "dict"
        if _typed_dict_class(modules, name) is not None:
            return kind == "dict"
        alias = _alias_value(modules, near, name)
        if alias is not None:
            return accepts(modules, alias[0], alias[1], kind, depth + 1)
        return None
    return None


def _any_of(results: list[bool | None]) -> bool | None:
    if any(item is True for item in results):
        return True
    if any(item is None for item in results):
        return None
    return False


def _alias_value(modules: dict[str, ast.Module], near: str, name: str) -> tuple[str, ast.AST] | None:
    """A module-level ``Name = <type>`` alias, looked up through the module's imports first."""
    order = [near] + [other for other in modules if other != near]
    tree = modules.get(near)
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and any(
                (alias.asname or alias.name) == name for alias in node.names
            ):
                source = _absolute_import(near, node, modules)
                if source in modules:
                    order.insert(0, source)
    for module_name in order:
        for stmt in modules.get(module_name, ast.Module(body=[], type_ignores=[])).body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in stmt.targets
            ):
                return module_name, stmt.value
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == name
                and stmt.value is not None
            ):
                return module_name, stmt.value
    return None


def _absolute_import(module: str, node: ast.ImportFrom, modules: dict[str, ast.Module]) -> str:
    if not node.level:
        return node.module or ""
    base = module.split(".")
    if not any(other.startswith(module + ".") for other in modules):
        base = base[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _typed_dict_fields(
    modules: dict[str, ast.Module], near: str, annotation: ast.AST, depth: int = 0
) -> list[str]:
    out: list[str] = []
    for sub in ast.walk(annotation):
        name = _type_name(sub) if isinstance(sub, (ast.Name, ast.Attribute)) else ""
        klass = _typed_dict_class(modules, name) if name else None
        if klass is None:
            alias = _alias_value(modules, near, name) if name and depth < 3 else None
            if alias is not None:
                out.extend(
                    entry
                    for entry in _typed_dict_fields(modules, alias[0], alias[1], depth + 1)
                    if entry not in out
                )
            continue
        fields = [
            f"{stmt.target.id}: {ast.unparse(stmt.annotation)[:80]}"
            for stmt in klass.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        ]
        entry = f"{name} is a TypedDict: " + ", ".join(fields)
        if entry not in out:
            out.append(entry)
    return out[:3]


def infer_kind(expr: ast.AST, tree: ast.Module, func: ast.AST | None, line: int, depth: int = 0) -> str | None:
    """Builtin kind of a value when the local code makes it obvious; else None."""
    if depth > _MAX_DEPTH:
        return None
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, bool):
            return "bool"
        if expr.value is None:
            return None
        name = type(expr.value).__name__
        return name if name in _BUILTINS else None
    if isinstance(expr, ast.JoinedStr):
        return "str"
    if isinstance(expr, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(expr, ast.Tuple):
        return "tuple"
    if isinstance(expr, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(expr, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
        left = infer_kind(expr.left, tree, func, line, depth + 1)
        if left == "str" or (isinstance(expr.op, ast.Add) and infer_kind(expr.right, tree, func, line, depth + 1) == "str"):
            return "str"
        return None
    if isinstance(expr, ast.Name) and func is not None:
        for arg in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs):
            if arg.arg == expr.id:
                return _builtin_of(arg.annotation)
        value = _last_assignment(func, expr.id, line)
        if value is None:
            return None
        if isinstance(value, ast.AST) and not isinstance(value, ast.expr):
            return _builtin_of(value.annotation)  # AnnAssign
        return infer_kind(value, tree, func, getattr(value, "lineno", line), depth + 1)
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Name) and expr.func.id in _BUILTINS:
            return expr.func.id
        name = expr.func.attr if isinstance(expr.func, ast.Attribute) else getattr(expr.func, "id", "")
        defs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ]
        kinds = {_builtin_of(node.returns) for node in defs}
        if len(kinds) == 1:
            return kinds.pop()
    return None


def _builtin_of(annotation: ast.AST | None) -> str | None:
    if annotation is None:
        return None
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value.strip(), mode="eval").body
        except SyntaxError:
            return None
    if isinstance(annotation, ast.Subscript):
        base = _type_name(annotation.value)
        return {"List": "list", "list": "list", "Dict": "dict", "dict": "dict", "Tuple": "tuple", "tuple": "tuple"}.get(base)
    if isinstance(annotation, ast.Name) and annotation.id in _BUILTINS:
        return annotation.id
    return None


def _last_assignment(func: ast.AST, name: str, line: int):
    best = None
    for node in ast.walk(func):
        lineno = getattr(node, "lineno", 0)
        if lineno >= line:
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                if best is None or lineno > best[0]:
                    best = (lineno, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if best is None or lineno > best[0]:
                best = (lineno, node)
        elif isinstance(node, (ast.AugAssign, ast.For, ast.With)):
            continue
    return best[1] if best else None


def _call_at(tree: ast.Module, line: int, expr: str) -> ast.Call | None:
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and node.lineno == line]
    for call in calls:
        try:
            if ast.unparse(call.func) == expr:
                return call
        except (TypeError, ValueError):
            continue
    tail = expr.rsplit(".", 1)[-1]
    for call in calls:
        if isinstance(call.func, ast.Attribute) and call.func.attr == tail:
            return call
    return None


def _enclosing_function(tree: ast.Module, line: int) -> ast.AST | None:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or node.lineno >= best.lineno:
                best = node
    return best


def _owner(dotted: str) -> str:
    return ".".join(dotted.split(".")[-2:])


def original_arguments(source: str, site, old_modules: dict[str, ast.Module]) -> set[str]:
    """Parameter names the site's original call passed: its keywords, plus
    positionals named through the old release's signature when it resolves."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    call = _call_at(tree, site.line, (site.expr or "").strip())
    if call is None or any(kw.arg is None for kw in call.keywords):
        return set()
    names = {kw.arg for kw in call.keywords if kw.arg}
    found = resolve_callable(old_modules, site.symbol) if call.args and site.symbol and old_modules else None
    if found is not None and not any(isinstance(arg, ast.Starred) for arg in call.args):
        order = [arg.arg for arg in (*found[1].args.posonlyargs, *found[1].args.args)]
        order = order[1:] if order and order[0] in {"self", "cls"} else order
        names |= set(order[: len(call.args)])
    return names


def dropped_arguments(
    modules: dict[str, ast.Module],
    tree: ast.Module,
    path: str,
    chain,
    passed_before: set[str],
    package: str,
    version: str,
) -> list[str]:
    """Arguments the original call passed that the rewrite's target still accepts but no longer gets.

    Only for a call that keeps at least one of the original arguments, so an
    unrelated new call in the rewritten function is not compared."""
    if not passed_before:
        return []
    found = resolve_callable(modules, chain.sdk_method)
    call = _call_at(tree, chain.call_lineno, chain.call_expr)
    if found is None or call is None or any(kw.arg is None for kw in call.keywords):
        return []
    node = found[1]
    order = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
    order = order[1:] if order and order[0] in {"self", "cls"} else order
    accepted = _accepted_names(modules, found[0], node)
    # ``**kwargs`` (on the implementation or an overload) accepts every name.
    if accepted is None:
        accepted = set(passed_before)
    passed = {kw.arg for kw in call.keywords if kw.arg} | set(order[: len(call.args)])
    if not passed & passed_before:
        return []
    return [
        f"{path}:{call.lineno} {chain.call_expr}: rewrite drops {name} which "
        f"{_owner(chain.sdk_method)} in {package} {version} still accepts; keep passing it"
        for name in sorted((passed_before & accepted) - passed)
    ]


def _accepted_names(modules: dict[str, ast.Module], module_name: str, node: ast.AST) -> set[str] | None:
    """Parameter names the callable accepts, or None when a variant takes ``**kwargs``.

    The implementation and its ``@overload`` siblings are one signature: a
    keyword-only name that exists only on an overload is still accepted.
    """
    variants = [node]
    tree = modules.get(module_name)
    if tree is not None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for klass in ast.walk(tree):
            if isinstance(klass, ast.ClassDef) and any(child is node for child in klass.body):
                variants = [
                    child
                    for child in klass.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == node.name
                ]
                break
    names: set[str] = set()
    for variant in variants:
        args = getattr(variant, "args", None)
        if args is None:
            continue
        if args.kwarg is not None:
            return None
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if arg.arg not in {"self", "cls"}:
                names.add(arg.arg)
    return names


# --- 2. attributes read on a return value --------------------------------


def attribute_breaks(
    modules: dict[str, ast.Module],
    root: Path,
    path: str,
    source: str,
    span: tuple[int, int],
    chains,
    package: str,
    version: str,
    flows: list[dict] | None = None,
) -> list[str]:
    """Attributes read on a value typed by the new release that its class lacks.

    ``flows``, when given, collects each release value this span passes into a
    repo function (see ``_callee_reads``), so a later rewrite of that function
    can be told what it now receives.

    The value is a call into the release (local variable or argument passed to
    the rewritten function by a caller in this repo), or a parameter annotated
    with one of the release's classes.
    """
    tree = ast.parse(source)
    top = package.lower().replace("-", "_")
    trees = {path: tree}
    if flows is None:
        flows = FLOWS.get()
    out: list[str] = []
    for func in _functions_in_span(tree, span):
        typed: dict[str, list[tuple[str, ast.ClassDef]]] = {}
        for index, arg in enumerate(_positional(func)):
            classes = _annotation_classes(modules, tree, arg.annotation, top)
            if classes or not _annotated_elsewhere(arg.annotation, top):
                passed = _caller_classes(modules, root, trees, chains, path, func, index, arg.arg)
                if classes and passed and not {k.name for _m, k in classes} & {k.name for _m, k in passed}:
                    # The annotation names a release class the callers never pass.
                    out.append(
                        f"{path}:{arg.lineno} {arg.arg}: {ast.unparse(arg.annotation)}: annotated "
                        f"{', '.join(k.name for _m, k in classes)} but callers pass "
                        f"{', '.join(k.name for _m, k in passed)} from {package} {version} "
                        f"{_fields_note(passed)}"
                    )
                    classes = passed
                classes = classes or passed
            if classes:
                typed[arg.arg] = classes
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                classes = _call_classes(modules, chains, path, node.value)
                if classes:
                    typed[node.targets[0].id] = classes
        for node in ast.walk(func):
            # ``for block in result.content`` / comprehensions: the element type of the field.
            if isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name):
                classes = _element_classes(modules, typed, node.iter)
                if classes:
                    typed[node.target.id] = classes
        parents = {child: parent for parent in ast.walk(func) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(func):
            name, attr = _attribute_read(node)
            classes = typed.get(name, []) if name else []
            if not name:
                # ``getattr(result.usage, "x", 0)``: the chain's type, field by field.
                name, attr, classes = _chain_guard(modules, typed, node)
            if not name or not classes or attr.startswith("__"):
                continue
            if _filters_items(parents, node, name) and _has_attribute(modules, classes, attr) is True:
                # Items without ``attr`` are skipped, and some item types have it:
                # nothing is made up for the others.
                continue
            masked = _masked_read(modules, path, node, name, attr, classes, package, version)
            if masked:
                if masked not in out:
                    out.append(masked)
                continue
            if _has_attribute(modules, classes, attr) is not False:
                continue
            names = ", ".join(sorted({klass.name for _module, klass in classes}))
            fields = _fields(classes)
            text = (
                f"{path}:{node.lineno} {ast.unparse(node)[:80]}: {name} is {names} in {package} "
                f"{version}, which has no attribute {attr}"
            )
            if fields:
                text += f" {_fields_note(classes)}"
            if isinstance(node, ast.Call):
                text += f"; a {node.func.id} check on it is dead code, drop that branch"
            if text not in out:
                out.append(text)
        for text in _callee_reads(modules, root, trees, chains, path, func, typed, package, version, flows):
            if text not in out:
                out.append(text)
        from conduit.apply.nonecheck import NONE_CHECK, none_breaks

        if NONE_CHECK.get():
            for text in none_breaks(modules, root, trees, path, tree, func, typed, package, version):
                if text not in out:
                    out.append(text)
    return out


def _chain_guard(modules, typed, node: ast.AST) -> tuple[str, str, list[tuple[str, ast.ClassDef]]]:
    """``getattr``/``hasattr`` on an attribute chain rooted in a typed name: (chain, attr, its classes)."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr"}
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Attribute)
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return "", "", []
    classes = _expr_classes(modules, typed, node.args[0], 0)
    return (ast.unparse(node.args[0]), node.args[1].value, classes) if classes else ("", "", [])


def _expr_classes(modules, typed, expr: ast.AST, depth: int) -> list[tuple[str, ast.ClassDef]]:
    """Release classes of ``name.a.b``: each field's annotation in the new release."""
    if isinstance(expr, ast.Name):
        return typed.get(expr.id, [])
    if not isinstance(expr, ast.Attribute) or depth > _MAX_DEPTH:
        return []
    out: list[tuple[str, ast.ClassDef]] = []
    for module_name, klass in _expr_classes(modules, typed, expr.value, depth + 1):
        found = _field_annotation(modules, module_name, klass, expr.attr, 0)
        if found is None:
            return []  # one possible owner does not declare it: unknown, stay quiet
        for item in _type_classes(modules, found[0], found[1], 0):
            if item not in out:
                out.append(item)
    return out


def repo_call_breaks(root: Path, path: str, source: str, span: tuple[int, int]) -> list[str]:
    """Calls in the span to a class or function this repo defines, checked against its own
    signature: a keyword it does not take, or a required argument left out."""
    tree = ast.parse(source)
    trees = {path: tree}
    defs: dict[str, list[ast.AST]] = {}
    by_file: dict[str, dict[str, ast.AST]] = {}
    for rel, module in _repo_trees(root, trees):
        for node in module.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append(node)
                by_file.setdefault(rel, {})[node.name] = node
    # The definition this module sees: its own, or the one it imports by name.
    seen: dict[str, ast.AST] = {
        node.name: node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            base = path.rsplit("/", 1)[0].split("/") if "/" in path else []
            if node.level:
                parts = base[: len(base) - (node.level - 1)] if node.level > 1 else base
            else:
                parts = []
            parts = parts + (node.module.split(".") if node.module else [])
            for alias in node.names:
                for rel in ("/".join(parts) + ".py", "/".join(parts) + "/__init__.py"):
                    hit = by_file.get(rel, {}).get(alias.name)
                    if hit is not None:
                        seen[alias.asname or alias.name] = hit
    out: list[str] = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and span[0] <= call.lineno <= span[1]):
            continue
        if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
            continue
        found = [seen[call.func.id]] if call.func.id in seen else defs.get(call.func.id, [])
        if len(found) != 1:
            continue
        target = found[0]
        if isinstance(target, ast.ClassDef):
            init = next(
                (stmt for stmt in target.body if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"), None
            )
            if init is None or target.decorator_list:
                continue  # inherited or generated constructor: unknown
            args, skip = init.args, 1
        else:
            args, skip = target.args, 0
        if args.kwarg is not None:
            continue
        positional = [arg.arg for arg in (*args.posonlyargs, *args.args)][skip:]
        names = set(positional[len(args.posonlyargs):]) | {arg.arg for arg in args.kwonlyargs}
        where = f"{path}:{call.lineno} {ast.unparse(call.func)}(...)"
        unknown = [kw.arg for kw in call.keywords if kw.arg not in names]
        if unknown:
            out.append(
                f"{where}: {call.func.id} in this repo takes no keyword {', '.join(unknown)} "
                f"(its parameters: {', '.join(positional + [arg.arg for arg in args.kwonlyargs]) or 'none'})"
            )
            continue
        if args.vararg is None and len(call.args) > len(positional):
            out.append(f"{where}: {call.func.id} in this repo takes {len(positional)} positional arguments, the rewrite passes {len(call.args)}")
            continue
        required = positional[: len(positional) - len(args.defaults)]
        given = set(positional[: len(call.args)]) | {kw.arg for kw in call.keywords}
        missing = [name for name in required if name not in given]
        missing += [arg.arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None and arg.arg not in given]
        if missing:
            out.append(f"{where}: {call.func.id} in this repo requires {', '.join(missing)}, which the rewrite does not pass")
    return out


def _filters_items(parents: dict[ast.AST, ast.AST], node: ast.AST, name: str) -> bool:
    """``node`` is a ``hasattr``/``getattr`` guard that only skips items of a loop or comprehension.

    It is the condition (alone, or under ``and`` / ``not``) of a comprehension's
    ``if`` over ``name``, or of an ``if`` inside a ``for`` over ``name`` whose
    chain has no ``else`` (a negated guard must just ``continue``). A guard with
    an ``else``, a conditional expression, or a ``getattr`` default used as a
    value supplies a stand-in value instead, and is not a filter."""
    if not isinstance(node, ast.Call):
        return False
    root, negated = node, False
    while isinstance(parents.get(root), (ast.BoolOp, ast.UnaryOp)):
        parent = parents[root]
        if isinstance(parent, ast.BoolOp) and not isinstance(parent.op, ast.And):
            return False
        if isinstance(parent, ast.UnaryOp):
            if not isinstance(parent.op, ast.Not):
                return False
            negated = not negated
        root = parent
    holder = parents.get(root)
    if isinstance(holder, ast.comprehension):
        return root in holder.ifs and _targets(holder.target, name)
    if not isinstance(holder, ast.If) or holder.test is not root:
        return False
    if negated:
        if not (len(holder.body) == 1 and isinstance(holder.body[0], ast.Continue) and not holder.orelse):
            return False
    elif _chain_has_else(holder):
        return False
    loop = parents.get(holder)
    while loop is not None and not isinstance(loop, (ast.For, ast.AsyncFor)):
        if not isinstance(loop, ast.If):
            return False
        loop = parents.get(loop)
    return loop is not None and _targets(loop.target, name)


def _chain_has_else(node: ast.If) -> bool:
    if not node.orelse:
        return False
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        return _chain_has_else(node.orelse[0])
    return True


def _targets(target: ast.AST, name: str) -> bool:
    return isinstance(target, ast.Name) and target.id == name


def _masked_read(modules, path: str, node: ast.AST, name: str, attr: str, classes, package: str, version: str) -> str:
    """A ``getattr(x, "a", default)`` / ``hasattr(x, "a")`` on a value whose type the release declares.

    When some of its possible classes lack ``a`` the default hides that case; when
    every class declares ``a`` the fallback is dead code that would hide a wrong
    value. Either way the rewrite should read the declared type, not guess."""
    if not isinstance(node, ast.Call):
        return ""
    func = node.func.id
    if func == "getattr" and len(node.args) < 3:
        return ""
    results = [(klass.name, _class_has(modules, module_name, klass, attr, 0)) for module_name, klass in classes]
    have = sorted({klass for klass, hit in results if hit is True})
    lack = sorted({klass for klass, hit in results if hit is False})
    unknown = any(hit is None for _klass, hit in results)
    where = f"{path}:{node.lineno} {ast.unparse(node)[:80]}"
    guard = "default" if func == "getattr" else "check"
    if have and lack:
        return (
            f"{where}: {name} may be {_few(lack)} in {package} {version}, which lack "
            f"{attr}; the {func} {guard} hides that, handle those types explicitly"
        )
    if have and not unknown:
        from conduit.apply.nonecheck import allows_none, field_decl

        declared = [field_decl(modules, module_name, klass, attr) for module_name, klass in classes]
        maybe_none = any(item is not None and allows_none(item[0]) for item in declared)
        text = (
            f"{where}: {name} is {', '.join(have)} in {package} {version}, which always has {attr}; "
            f"the {func} {guard} is dead code that would hide a wrong value, read {name}.{attr} directly"
        )
        if maybe_none:
            # The attribute is always there, but its value may be None: handling that
            # (``or 0``, an is None check) is not masking.
            text += f" (its value may be None: handle that with `{name}.{attr} or ...` or an is None check)"
        return text
    return ""


def _few(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _element_classes(modules, typed, iterable: ast.AST) -> list[tuple[str, ast.ClassDef]]:
    """Classes of the items of ``name.field`` when the field is annotated as a one-argument container."""
    if not (isinstance(iterable, ast.Attribute) and isinstance(iterable.value, ast.Name)):
        return []
    out: list[tuple[str, ast.ClassDef]] = []
    for module_name, klass in typed.get(iterable.value.id, []):
        found = _field_annotation(modules, module_name, klass, iterable.attr, 0)
        if found is None:
            continue
        near, annotation = found
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value.strip(), mode="eval").body
            except SyntaxError:
                continue
        if not isinstance(annotation, ast.Subscript) or isinstance(annotation.slice, ast.Tuple):
            continue
        for item in _type_classes(modules, near, annotation.slice, 0):
            if item not in out:
                out.append(item)
    return out


def _field_annotation(modules, module_name: str, klass: ast.ClassDef, attr: str, depth: int):
    for stmt in _class_statements(klass.body):
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == attr:
            return module_name, stmt.annotation
    if depth > 4:
        return None
    for base in klass.bases:
        name = _type_name(base.value if isinstance(base, ast.Subscript) else base)
        hit = _find_class(modules, name, module_name) if name else None
        if hit is not None and hit[1] is not klass:
            found = _field_annotation(modules, hit[0], hit[1], attr, depth + 1)
            if found is not None:
                return found
    return None


def _type_classes(modules, near: str, node: ast.AST, depth: int) -> list[tuple[str, ast.ClassDef]]:
    """Release classes a type expression can be: unions and aliases expanded, metadata skipped."""
    if depth > _MAX_DEPTH or node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _type_classes(modules, near, node.left, depth + 1) + _type_classes(modules, near, node.right, depth + 1)
    if isinstance(node, ast.Subscript):
        parts = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if _type_name(node.value) == "Annotated":  # typing's own form: metadata after the type
            parts = parts[:1]
        out: list[tuple[str, ast.ClassDef]] = []
        for part in parts:
            for item in _type_classes(modules, near, part, depth + 1):
                if item not in out:
                    out.append(item)
        return out
    name = _type_name(node)
    if not name or name in _ANY or name == "None":
        return []
    alias = _alias_value(modules, near, name)
    if alias is not None and not isinstance(alias[1], ast.Call):
        return _type_classes(modules, alias[0], alias[1], depth + 1)
    hit = _find_class(modules, name, near)
    return [hit] if hit else []


def _callee_reads(
    modules, root: Path, trees, chains, path: str, func, typed, package: str, version: str, flows: list[dict] | None = None
) -> list[str]:
    """Attributes a repo function reads on a release value this function passes it.

    ``helper(result)`` where ``result`` comes from the release: the helper's
    parameter has that type, so ``param.x`` / ``getattr(param, "x", default)``
    there must exist on it too (the default does not make it work)."""
    out: list[str] = []
    for call in ast.walk(func):
        if not isinstance(call, ast.Call):
            continue
        callee = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
        if not callee or callee in {"getattr", "hasattr", "isinstance"}:
            continue
        given: list[tuple[int | str, list[tuple[str, ast.ClassDef]]]] = []
        for index, value in [*enumerate(call.args), *((kw.arg, kw.value) for kw in call.keywords if kw.arg)]:
            classes = typed.get(value.id, []) if isinstance(value, ast.Name) else _call_classes(modules, chains, path, value)
            if classes:
                given.append((index, classes))
        if not given:
            continue
        defs = [
            (rel, tree, node)
            for rel, tree in _repo_trees(root, trees)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == callee
        ]
        if len(defs) != 1:
            continue
        rel, home, target = defs[0]
        params = _positional(target)
        for index, classes in given:
            param = params[index] if isinstance(index, int) and index < len(params) else next(
                (arg for arg in (*params, *target.args.kwonlyargs) if arg.arg == index), None
            )
            if param is None:
                continue
            if _annotated_elsewhere(param.annotation, "") and not _stale_annotation(modules, home, param.annotation, package):
                continue
            names = ", ".join(sorted({klass.name for _module, klass in classes}))
            if flows is not None:
                record = {
                    "callee_path": rel,
                    "callee": target.name,
                    "start": min([target.lineno, *[deco.lineno for deco in target.decorator_list]]),
                    "end": target.end_lineno or target.lineno,
                    "param": param.arg,
                    "types": names,
                    "fields": _fields(classes),
                    "from": f"{path}:{call.lineno}",
                    "release": f"{package} {version}",
                    "imports": _import_lines_for(modules, classes, package),
                }
                if record not in flows:
                    flows.append(record)
            for node in ast.walk(target):
                name, attr = _attribute_read(node)
                if name != param.arg or attr.startswith("__") or _has_attribute(modules, classes, attr) is not False:
                    continue
                text = (
                    f"{rel}:{node.lineno} {ast.unparse(node)[:80]}: {target.name}() gets {param.arg} = {names} "
                    f"from {path}:{call.lineno} in {package} {version}, which has no attribute {attr}"
                )
                fields = _fields(classes)
                if fields:
                    text += f" {_fields_note(classes)}; read what you need here instead of passing it on"
                if text not in out:
                    out.append(text)
    return out


def _import_lines_for(modules, classes, package: str) -> list[str]:
    """``from <module> import <Name>`` for each class, from the shortest module of
    the release that binds the name at its top (its public path)."""
    top = package.lower().replace("-", "_")
    out: list[str] = []
    for module_name, klass in classes:
        homes = [
            name for name, tree in modules.items()
            if name.split(".")[0] == top and not any(part.startswith("_") for part in name.split("."))
            and _module_binds(tree, klass.name)
        ] or [module_name]
        home = min(homes, key=lambda name: (name.count("."), len(name), name))
        line = f"from {home} import {klass.name}"
        if line not in out:
            out.append(line)
    return out


def _stale_annotation(modules, tree: ast.Module, annotation: ast.AST | None, package: str) -> bool:
    """An annotation that names nothing real any more: its name is bound nowhere in
    the module (the import was removed with the old release), or it is imported
    from the release, which no longer defines it. It says nothing about the value,
    so the value the caller passes decides."""
    node = annotation
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return False
    if isinstance(node, ast.Subscript):
        node = node.value
    head = node
    while isinstance(head, ast.Attribute):
        head = head.value
    if not isinstance(head, ast.Name):
        return False
    name = head.id
    import builtins

    if hasattr(builtins, name):
        return False
    top = package.lower().replace("-", "_")
    bound = False
    for item in ast.walk(tree):
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            for alias in item.names:
                if (alias.asname or alias.name.split(".")[0]) != name:
                    continue
                bound = True
                module = item.module if isinstance(item, ast.ImportFrom) else ""
                if (
                    isinstance(item, ast.ImportFrom) and not item.level and module
                    and module.split(".")[0] == top and node is head
                    and not _module_binds(modules.get(module), alias.name)
                ):
                    return True
        elif isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
            bound = True
        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                bound = True
    return not bound


def _module_binds(tree: ast.Module | None, name: str) -> bool:
    """``name`` is defined or re-exported at the top of a release module."""
    if tree is None:
        return False
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name) == name for a in node.names):
            return True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return True
    return False


def _fields_note(classes) -> str:
    """``(Name fields: a: int, ...)``: the type is named so a report can list it once."""
    names = ", ".join(sorted({klass.name for _module, klass in classes}))
    return f"({names} fields: {', '.join(_fields(classes))})"


def _functions_in_span(tree: ast.Module, span: tuple[int, int]) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and span[0] <= node.lineno <= span[1]
    ]


def _positional(func: ast.AST) -> list[ast.arg]:
    args = [*func.args.posonlyargs, *func.args.args]
    decorators = {_type_name(deco) for deco in func.decorator_list}
    if "staticmethod" not in decorators and args and args[0].arg in {"self", "cls"}:
        args = args[1:]
    return args


def _annotated_elsewhere(annotation: ast.AST | None, top: str) -> bool:
    """An annotation naming some other concrete type: trust it, do not look at callers."""
    if annotation is None:
        return False
    return _type_name(annotation) not in _ANY | {""}


def _annotation_classes(
    modules: dict[str, ast.Module], tree: ast.Module, annotation: ast.AST | None, top: str
) -> list[tuple[str, ast.ClassDef]]:
    if annotation is None:
        return []
    node = annotation
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return []
    dotted = ast.unparse(node) if isinstance(node, (ast.Name, ast.Attribute)) else ""
    if not dotted:
        return []
    if "." in dotted:
        module, _, name = dotted.rpartition(".")
    else:
        module, name = _imported_from(tree, dotted), dotted
    if not module or module.split(".")[0] != top:
        return []
    hit = _find_class(modules, name, module)
    return [hit] if hit else []


def _imported_from(tree: ast.Module, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if any((alias.asname or alias.name) == name for alias in node.names):
                return node.module
    return ""


def _call_classes(modules, chains, path: str, expr: ast.AST) -> list[tuple[str, ast.ClassDef]]:
    """Return classes of a call into the release at ``expr`` (resolved by the service map)."""
    if not isinstance(expr, ast.Call):
        return []
    for chain in chains:
        if (
            chain.call_rel.replace("\\", "/") == path
            and chain.call_lineno == expr.lineno
            and chain.kind.endswith("_call")
            and not chain.via_wrapper
        ):
            found = resolve_callable(modules, chain.sdk_method)
            if found is None:
                return []
            module_name, node = found
            returns = _matching_overload_returns(modules, module_name, node, expr)
            return _return_classes(modules, module_name, node, returns)
    return []


def _matching_overload_returns(modules, module_name: str, node: ast.AST, call: ast.Call) -> list[ast.AST]:
    """Return annotations of the ``@overload`` variants this call's keywords can select.

    A variant fits when every keyword the call passes is one of its parameters and
    every parameter it requires (no default) is passed. Empty when none narrow it.
    """
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
        return []
    passed = {kw.arg for kw in call.keywords}
    owner = next(
        (
            klass
            for klass in ast.walk(modules.get(module_name, ast.Module(body=[], type_ignores=[])))
            if isinstance(klass, ast.ClassDef) and node in klass.body
        ),
        None,
    )
    if owner is None:
        return []
    variants = [
        child
        for child in owner.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name == node.name
        and _is_overload(child)
    ]
    fits: list[ast.AST] = []
    for variant in variants:
        args = variant.args
        positional = [arg.arg for arg in (*args.posonlyargs, *args.args)][1:]  # drop self
        names = set(positional) | {arg.arg for arg in args.kwonlyargs}
        if args.kwarg is None and not passed <= names:
            continue
        required_kwonly = {
            arg.arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None
        }
        required_pos = positional[len(call.args) : len(positional) - len(args.defaults)]
        if not (required_kwonly | set(required_pos)) <= passed:
            continue
        if variant.returns is not None:
            fits.append(variant.returns)
    return fits if 0 < len(fits) < len(variants) else []


def _return_classes(
    modules, module_name: str, node: ast.AST, narrowed: list[ast.AST] | None = None
) -> list[tuple[str, ast.ClassDef]]:
    returns = getattr(node, "returns", None)
    if returns is None:
        return []
    out: list[tuple[str, ast.ClassDef]] = []
    for sub in (item for tree in (narrowed or [returns]) for item in ast.walk(tree)):
        name = ""
        if isinstance(sub, (ast.Name, ast.Attribute)):
            name = _type_name(sub)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            name = sub.value.strip().split("[", 1)[0].rsplit(".", 1)[-1]
        if not name or name in _ANY or name == "None":
            continue
        hit = _find_class(modules, name, module_name)
        if hit is not None and hit not in out:
            out.append(hit)
    # A return type naming nothing the wheel defines is unknown.
    if not out:
        return []
    return out


def _caller_classes(modules, root: Path, trees, chains, path: str, func, index: int, name: str):
    """Classes of a parameter from callers in this repo that pass a release call's result."""
    out: list[tuple[str, ast.ClassDef]] = []
    unknown = False
    for rel, tree in _repo_trees(root, trees):
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            callee = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
            if callee != func.name:
                continue
            value = call.args[index] if index < len(call.args) else next(
                (kw.value for kw in call.keywords if kw.arg == name), None
            )
            if value is None:
                continue
            if isinstance(value, ast.Name):
                caller = _enclosing_function(tree, call.lineno)
                value = _last_assignment(caller, value.id, call.lineno) if caller is not None else None
            classes = _call_classes(modules, chains, rel, value) if value is not None else []
            if not classes:
                unknown = True
                continue
            out.extend(item for item in classes if item not in out)
    # Every caller must agree that the value comes from the release.
    return [] if unknown else out


def _repo_trees(root: Path, cached: dict[str, ast.Module]):
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        if any(part in {"tests", "test", ".venv", "venv", "node_modules"} or part.startswith(".") for part in rel.split("/")[:-1]):
            continue
        if rel in cached:
            yield rel, cached[rel]
            continue
        try:
            cached[rel] = ast.parse(file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        yield rel, cached[rel]


def _attribute_read(node: ast.AST) -> tuple[str, str]:
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr"}
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[0].id, node.args[1].value
    return "", ""


def _has_attribute(modules, classes, attr: str) -> bool | None:
    results = [_class_has(modules, module_name, klass, attr, 0) for module_name, klass in classes]
    return _any_of(results)


def _class_statements(body: list[ast.stmt]):
    """Class-body statements, including every branch of ``if`` / ``try`` blocks."""
    for stmt in body:
        if isinstance(stmt, ast.If):
            yield from _class_statements(stmt.body)
            yield from _class_statements(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            for part in (stmt.body, stmt.orelse, stmt.finalbody, *[h.body for h in stmt.handlers]):
                yield from _class_statements(part)
        else:
            yield stmt


def _class_has(modules, module_name: str, klass: ast.ClassDef, attr: str, depth: int) -> bool | None:
    for stmt in klass.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "__getattr__":
            return None  # unconditional dynamic lookup: anything may exist
    for stmt in _class_statements(klass.body):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == attr:
            return True
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == attr:
            return True
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == attr for target in stmt.targets
        ):
            return True
    if depth > 4:
        return None
    results: list[bool | None] = []
    for base in klass.bases:
        node = base.value if isinstance(base, ast.Subscript) else base
        name = _type_name(node)
        hit = _find_class(modules, name, module_name) if name else None
        if hit is not None and hit[1] is not klass:
            results.append(_class_has(modules, hit[0], hit[1], attr, depth + 1))
            continue
        results.append(_external_has(modules, module_name, node, attr))
    if not results:
        return False
    return _any_of(results)


def _external_has(modules, module_name: str, base: ast.AST, attr: str) -> bool | None:
    """A base class from another installed distribution: ask that class itself."""
    import importlib

    dotted = ast.unparse(base)
    name = dotted.rsplit(".", 1)[-1]
    source = dotted.rpartition(".")[0]
    tree = modules.get(module_name)
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and not node.level and node.module and not source:
                for alias in node.names:
                    if (alias.asname or alias.name) == name:
                        source, name = node.module, alias.name
            elif isinstance(node, ast.Import) and source:
                for alias in node.names:
                    if (alias.asname or alias.name) == source.split(".")[0]:
                        source = alias.name + source[len(source.split(".")[0]):]
    if not source:
        return None
    try:
        klass = getattr(importlib.import_module(source), name)
    except Exception:  # noqa: BLE001 — not installed or not importable: unknown
        return None
    if not isinstance(klass, type):
        return None
    if attr in dir(klass):
        return True
    for owner in klass.__mro__:
        if "__getattr__" in owner.__dict__ and owner is not object and not _guarded_getattr(owner):
            if _getattr_rejects_unknown(owner):
                # Raises for names it does not know: an undeclared attribute is missing.
                continue
            # Unconditional dynamic lookup: anything may exist.
            return None
    return False


def _getattr_rejects_unknown(klass: type) -> bool:
    """True when ``__getattr__`` raises AttributeError instead of answering every name."""
    import inspect
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(klass)))
    except (OSError, TypeError, SyntaxError):
        return False
    node = tree.body[0] if tree.body else None
    if not isinstance(node, ast.ClassDef):
        return False
    for stmt in node.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) or stmt.name != "__getattr__":
            continue
        return any(
            isinstance(sub, ast.Raise)
            and (
                sub.exc is None
                or _type_name(sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc) == "AttributeError"
            )
            for sub in ast.walk(stmt)
        )
    return False


def _guarded_getattr(klass: type) -> bool:
    """True when ``__getattr__`` is defined only under a condition (e.g. ``if not TYPE_CHECKING``),
    i.e. not part of the class's declared surface."""
    import inspect
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(klass)))
    except (OSError, TypeError, SyntaxError):
        return False
    node = tree.body[0] if tree.body else None
    if not isinstance(node, ast.ClassDef):
        return False
    return not any(
        isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "__getattr__"
        for stmt in node.body
    )


def _fields(classes) -> list[str]:
    names: list[str] = []
    for _module, klass in classes:
        for stmt in klass.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                text = f"{stmt.target.id}: {ast.unparse(stmt.annotation)[:60]}"
                if text not in names:
                    names.append(text)
    return names[:12]


# --- 3. what a new-release callable needs (for the first prompt) -----------

_MAX_BRIEF = 1200


def parameter_brief(modules: dict[str, ast.Module], dotted: str) -> str:
    """Required parameters of a new-release callable with their annotated types,
    a sketch of TypedDict-based types, and the other names it accepts.

    Read only from the wheel's code; ``""`` when the callable does not resolve."""
    found = resolve_callable(modules, dotted)
    if found is None:
        return ""
    module_name, node = found
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    args = node.args
    positional = [arg for arg in (*args.posonlyargs, *args.args) if arg.arg not in {"self", "cls"}]
    first_default = len([*args.posonlyargs, *args.args]) - len(args.defaults)
    required = [arg for index, arg in enumerate([*args.posonlyargs, *args.args]) if index < first_default and arg in positional]
    required += [arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None]
    parts: list[str] = []
    for arg in required:
        text = arg.arg
        if arg.annotation is not None:
            text += f": {ast.unparse(arg.annotation)[:120]}"
            shape = _shape(modules, module_name, arg.annotation)
            if shape:
                text += f", i.e. {shape}"
            fields = _typed_dict_fields(modules, module_name, arg.annotation)
            if fields:
                text += " (" + "; ".join(fields) + ")"
        parts.append(text)
    label = ".".join(dotted.split(".")[-2:])
    brief = f"{label} requires: " + ("; ".join(parts) if parts else "nothing")
    accepted = _accepted_names(modules, module_name, node)
    if accepted is None:
        brief += ". Also accepts any keyword (**kwargs)."
    else:
        others = sorted(accepted - {arg.arg for arg in required})
        if others:
            brief += ". Other parameters it accepts (no others): " + ", ".join(others)
    return brief[:_MAX_BRIEF]
