"""Static checks of a rewrite against the new release's own types.

Both checks read types only from the new wheel's code (and, for a base class
the wheel imports from another installed distribution, that class itself).
Anything that cannot be resolved is treated as unknown and never flagged.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conduit.apply.signatures import (
    _find_class,
    _is_overload,
    _type_name,
    _typed_dict_class,
    missing_member,
    resolve_callable,
)

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
) -> list[str]:
    """Attributes read on a value typed by the new release that its class lacks.

    The value is a call into the release (local variable or argument passed to
    the rewritten function by a caller in this repo), or a parameter annotated
    with one of the release's classes.
    """
    tree = ast.parse(source)
    top = package.lower().replace("-", "_")
    trees = {path: tree}
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
                        f"(fields: {', '.join(_fields(passed))})"
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
            name, attr = _attribute_read(node)
            if not name or name not in typed or attr.startswith("__"):
                continue
            classes = typed[name]
            if _has_attribute(modules, classes, attr) is not False:
                continue
            names = ", ".join(sorted({klass.name for _module, klass in classes}))
            fields = _fields(classes)
            text = (
                f"{path}:{node.lineno} {ast.unparse(node)[:80]}: {name} is {names} in {package} "
                f"{version}, which has no attribute {attr}"
            )
            if fields:
                text += f" (fields: {', '.join(fields)})"
            if isinstance(node, ast.Call):
                text += f"; a {node.func.id} check on it is dead code, drop that branch"
            if text not in out:
                out.append(text)
        for text in _callee_reads(modules, root, trees, chains, path, func, typed, package, version):
            if text not in out:
                out.append(text)
    return out


def _callee_reads(modules, root: Path, trees, chains, path: str, func, typed, package: str, version: str) -> list[str]:
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
            (rel, node)
            for rel, tree in _repo_trees(root, trees)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == callee
        ]
        if len(defs) != 1:
            continue
        rel, target = defs[0]
        params = _positional(target)
        for index, classes in given:
            param = params[index] if isinstance(index, int) and index < len(params) else next(
                (arg for arg in (*params, *target.args.kwonlyargs) if arg.arg == index), None
            )
            if param is None or _annotated_elsewhere(param.annotation, ""):
                continue
            names = ", ".join(sorted({klass.name for _module, klass in classes}))
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
                    text += f" (fields: {', '.join(fields)}); read what you need here instead of passing it on"
                if text not in out:
                    out.append(text)
    return out


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
