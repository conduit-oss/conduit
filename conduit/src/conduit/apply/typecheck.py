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
        return []
    module_name, node = found
    call = _call_at(tree, chain.call_lineno, chain.call_expr)
    if call is None:
        return []
    func = _enclosing_function(tree, call.lineno)
    params = {arg.arg: arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    out: list[str] = []
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
            yield rel, ast.parse(file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue


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
            # Unconditional dynamic lookup: anything may exist.
            return None
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


# --- 3. a joined multi-turn string stuffed into one record ----------------


def transcript_breaks(
    modules: dict[str, ast.Module],
    tree: ast.Module,
    path: str,
    chain,
    package: str,
    version: str,
) -> list[str]:
    """A role-transcript string placed into one record of a sequence of records.

    The parameter's element type comes from the new release: a record with a
    role field (a ``Literal`` / ``Enum`` of several strings) and a text field
    that accepts ``str``. The value is a transcript when same-file code joins
    a sequence into one string, picking a prefix per item with a conditional
    that does not read the item. That is the old "Human:/Assistant:" shape
    wrapped as a single message: type-correct, and still one turn.
    """
    found = resolve_callable(modules, chain.sdk_method)
    if found is None:
        return []
    module_name, node = found
    call = _call_at(tree, chain.call_lineno, chain.call_expr)
    if call is None:
        return []
    func = _enclosing_function(tree, call.lineno)
    params = {arg.arg: arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    out: list[str] = []
    for keyword in call.keywords:
        param = params.get(keyword.arg or "")
        if param is None or param.annotation is None or keyword.value is None:
            continue
        spec = _role_sequence(modules, module_name, param.annotation)
        if spec is None:
            continue
        role_field, allowed, text_fields = spec
        if not _wraps_transcript(keyword.value, tree, func, call.lineno, text_fields):
            continue
        shown = ", ".join(repr(item) for item in allowed)
        out.append(
            f"{path}:{call.lineno} {chain.call_expr}: {keyword.arg}= wraps a joined multi-turn "
            f"string into one record; {_owner(chain.sdk_method)} in {package} {version} expects "
            f"one record per turn ({role_field} is one of {shown}). Rewrite the helper that joins "
            f"those turns so each turn is its own record, and pass that sequence. Do not put the "
            f"joined string in a single record's text ({', '.join(text_fields)})."
        )
    return out


def _role_sequence(
    modules: dict[str, ast.Module], near: str, annotation: ast.AST | None, depth: int = 0
) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """``(role field, allowed values, text fields)`` when ``annotation`` is a sequence of those records."""
    element = _sequence_element(modules, near, annotation, depth)
    if element is None:
        return None
    return _role_record(modules, near, element, depth)


def _sequence_element(
    modules: dict[str, ast.Module], near: str, node: ast.AST | None, depth: int
) -> ast.AST | None:
    if node is None or depth > _MAX_DEPTH:
        return None
    node = _as_expr(node)
    if node is None:
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _sequence_element(modules, near, node.left, depth + 1) or _sequence_element(
            modules, near, node.right, depth + 1
        )
    if isinstance(node, ast.Subscript):
        base = _type_name(node.value)
        items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if base in _UNWRAP or base == "Union":
            found = None
            for item in items if base == "Union" else items[:1]:
                found = _sequence_element(modules, near, item, depth + 1)
                if found is not None:
                    return found
            return None
        if base in _ITERABLES and items:
            inner = items[0]
            # ``tuple[Turn, ...]`` — the element is the first entry, not the ellipsis.
            return inner
        return None
    if isinstance(node, (ast.Name, ast.Attribute)):
        alias = _alias_value(modules, near, _type_name(node))
        if alias is not None:
            return _sequence_element(modules, alias[0], alias[1], depth + 1)
    return None


def _role_record(
    modules: dict[str, ast.Module], near: str, node: ast.AST | None, depth: int
) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    if node is None or depth > _MAX_DEPTH:
        return None
    node = _unwrap_type(node)
    if node is None:
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _role_record(modules, near, node.left, depth + 1) or _role_record(
            modules, near, node.right, depth + 1
        )
    if isinstance(node, ast.Subscript) and _type_name(node.value) == "Union":
        items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        for item in items:
            found = _role_record(modules, near, item, depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(node, (ast.Name, ast.Attribute)):
        return None
    name = _type_name(node)
    alias = _alias_value(modules, near, name)
    if alias is not None and _class_def(modules, near, name) is None:
        return _role_record(modules, alias[0], alias[1], depth + 1)
    klass = _class_def(modules, near, name)
    if klass is None:
        return None
    role_name = ""
    allowed: tuple[str, ...] = ()
    texts: list[str] = []
    for stmt in _class_statements(klass.body):
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        inner = _unwrap_type(stmt.annotation)
        values = _literal_strings(inner)
        if not values and isinstance(inner, (ast.Name, ast.Attribute)):
            values = _enum_strings(modules, near, _type_name(inner))
        if len(values) >= 2 and not role_name:
            role_name = stmt.target.id
            allowed = values
            continue
        if _accepts_plain_str(stmt.annotation):
            texts.append(stmt.target.id)
    if role_name and texts:
        return role_name, allowed, tuple(texts)
    return None


def _class_def(modules: dict[str, ast.Module], near: str, name: str) -> ast.ClassDef | None:
    typed = _typed_dict_class(modules, name)
    if typed is not None:
        return typed
    hit = _find_class(modules, name, near)
    return hit[1] if hit else None


def _as_expr(node: ast.AST | None) -> ast.AST | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return None
    return node


def _unwrap_type(node: ast.AST | None, depth: int = 0) -> ast.AST | None:
    node = _as_expr(node)
    if node is None or depth > _MAX_DEPTH:
        return node
    if isinstance(node, ast.Subscript) and _type_name(node.value) in _UNWRAP:
        items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if items:
            return _unwrap_type(items[0], depth + 1)
    return node


def _literal_strings(node: ast.AST | None) -> tuple[str, ...]:
    node = _as_expr(node)
    if not isinstance(node, ast.Subscript) or _type_name(node.value) != "Literal":
        return ()
    elts = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
    return tuple(item.value for item in elts if isinstance(item, ast.Constant) and isinstance(item.value, str))


def _enum_strings(modules: dict[str, ast.Module], near: str, name: str) -> tuple[str, ...]:
    hit = _find_class(modules, name, near)
    if hit is None:
        return ()
    klass = hit[1]
    bases = [ast.unparse(base).split(".")[-1] for base in klass.bases]
    if not any(base in {"Enum", "StrEnum"} for base in bases):
        return ()
    values: list[str] = []
    for stmt in _class_statements(klass.body):
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.append(value.value)
    return tuple(values)


def _accepts_plain_str(node: ast.AST | None, depth: int = 0) -> bool:
    if node is None or depth > _MAX_DEPTH:
        return False
    node = _unwrap_type(node)
    if node is None:
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _accepts_plain_str(node.left, depth + 1) or _accepts_plain_str(node.right, depth + 1)
    if isinstance(node, ast.Subscript):
        base = _type_name(node.value)
        items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if base == "Union":
            return any(_accepts_plain_str(item, depth + 1) for item in items)
        return False
    return isinstance(node, ast.Name) and node.id == "str"


def _wraps_transcript(
    expr: ast.AST,
    tree: ast.AST,
    func: ast.AST | None,
    line: int,
    text_fields: tuple[str, ...],
    seen: set[int] | None = None,
    depth: int = 0,
) -> bool:
    """True when ``expr`` is a sequence containing a joined multi-turn string."""
    if depth > _MAX_DEPTH:
        return False
    expr = _follow_expr(expr, func, line)
    if isinstance(expr, (ast.List, ast.Tuple)):
        return any(_element_wraps(elt, tree, func, line, text_fields, seen, depth) for elt in expr.elts)
    if isinstance(expr, ast.IfExp):
        return _wraps_transcript(expr.body, tree, func, line, text_fields, seen, depth + 1) or _wraps_transcript(
            expr.orelse, tree, func, line, text_fields, seen, depth + 1
        )
    if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant):
        return _subscript_wraps(expr, tree, func, line, text_fields, seen, depth)
    if isinstance(expr, ast.Call):
        seen = set(seen or ())
        for helper in _functions_named(tree, _callee_name(expr)):
            if helper is func or id(helper) in seen:
                continue
            seen.add(id(helper))
            for ret in _return_exprs(helper):
                if _wraps_transcript(ret, tree, helper, getattr(ret, "lineno", line), text_fields, seen, depth + 1):
                    return True
        return False
    return False


def _subscript_wraps(expr, tree, func, line, text_fields, seen, depth) -> bool:
    key = expr.slice.value
    base = _follow_expr(expr.value, func, line)
    if isinstance(base, ast.Dict):
        for dict_key, value in zip(base.keys, base.values):
            if isinstance(dict_key, ast.Constant) and dict_key.value == key and value is not None:
                if _wraps_transcript(value, tree, func, line, text_fields, seen, depth + 1):
                    return True
        return False
    if isinstance(base, ast.Call):
        for helper in _functions_named(tree, _callee_name(base)):
            for ret in _return_exprs(helper):
                value = _follow_expr(ret, helper, getattr(ret, "lineno", line))
                if not isinstance(value, ast.Dict):
                    continue
                for dict_key, item in zip(value.keys, value.values):
                    if isinstance(dict_key, ast.Constant) and dict_key.value == key and item is not None:
                        if _wraps_transcript(
                            item, tree, helper, getattr(item, "lineno", line), text_fields, seen, depth + 1
                        ):
                            return True
    return False


def _element_wraps(elt, tree, func, line, text_fields, seen, depth) -> bool:
    if _is_transcript_expr(elt, tree, func, line):
        return True
    if isinstance(elt, ast.Dict):
        for key, value in zip(elt.keys, elt.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in text_fields
                and value is not None
                and _is_transcript_expr(value, tree, func, line)
            ):
                return True
    if isinstance(elt, ast.Call):
        for keyword in elt.keywords:
            if keyword.arg in text_fields and _is_transcript_expr(keyword.value, tree, func, line):
                return True
    return False


def _is_transcript_expr(expr: ast.AST | None, tree: ast.AST, func: ast.AST | None, line: int, depth: int = 0) -> bool:
    """True when ``expr`` is a string built by joining role-prefixed turns."""
    if expr is None or depth > _MAX_DEPTH:
        return False
    if isinstance(expr, ast.Name):
        if func is not None and _name_accumulates_roles(func, expr.id, line):
            return True
        value = _assigned_value(func, expr.id, line) if func is not None else None
        if isinstance(value, ast.expr):
            return _is_transcript_expr(value, tree, func, getattr(value, "lineno", line), depth + 1)
        return False
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _is_transcript_expr(expr.left, tree, func, line, depth + 1) or _is_transcript_expr(
            expr.right, tree, func, line, depth + 1
        )
    if not isinstance(expr, ast.Call):
        return False
    if _is_join_call(expr) and func is not None and _join_joins_role_turns(func, expr):
        return True
    for helper in _functions_named(tree, _callee_name(expr)):
        if helper is not func and _function_builds_transcript(helper):
            return True
    return False


def _function_builds_transcript(fn: ast.AST) -> bool:
    for ret in _return_exprs(fn):
        value = _follow_expr(ret, fn, getattr(ret, "lineno", 0))
        if isinstance(value, ast.Call) and _is_join_call(value) and _join_joins_role_turns(fn, value):
            return True
        if isinstance(ret, ast.Name) and _name_accumulates_roles(fn, ret.id, getattr(ret, "lineno", 0)):
            return True
    return False


def _join_joins_role_turns(fn: ast.AST, call: ast.Call) -> bool:
    if not call.args:
        return False
    arg = call.args[0]
    if isinstance(arg, ast.Name):
        return _name_fed_by_role_loop(fn, arg.id)
    if isinstance(arg, ast.GeneratorExp):
        targets: set[str] = set()
        for comp in arg.generators:
            targets |= _target_names(comp.target)
        return _contains_role_ifexp(arg.elt, targets)
    return False


def _name_fed_by_role_loop(fn: ast.AST, name: str) -> bool:
    for loop in _loops(fn):
        targets = _target_names(loop.target)
        prefixes = _prefix_names(loop, targets)
        for node in ast.walk(loop):
            added = _added_to_name(node, name)
            if added is None:
                continue
            if prefixes and _loads(added, prefixes):
                return True
            if _contains_role_ifexp(added, targets):
                return True
    return False


def _name_accumulates_roles(fn: ast.AST, name: str, line: int) -> bool:
    for loop in _loops(fn):
        if loop.lineno >= line:
            continue
        targets = _target_names(loop.target)
        prefixes = _prefix_names(loop, targets)
        for node in ast.walk(loop):
            if (
                isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and isinstance(node.op, ast.Add)
                and (prefixes and _loads(node.value, prefixes) or _contains_role_ifexp(node.value, targets))
            ):
                return True
    return False


def _loops(fn: ast.AST):
    for node in ast.walk(fn):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            yield node


def _prefix_names(loop: ast.AST, targets: set[str]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(loop):
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(value, ast.IfExp) and _role_ifexp(value, targets):
            targets_assigned = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets_assigned:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        if isinstance(node, ast.If) and _loads(node.test, targets):
            assigned = _if_assigns_prefix(node, targets)
            if assigned:
                names.add(assigned)
    return names


def _if_assigns_prefix(node: ast.If, targets: set[str]) -> str:
    def assigned(stmts: list[ast.stmt]) -> tuple[str, ast.AST] | None:
        if len(stmts) != 1 or not isinstance(stmts[0], ast.Assign) or len(stmts[0].targets) != 1:
            return None
        target = stmts[0].targets[0]
        if isinstance(target, ast.Name) and _is_foreign_prefix(stmts[0].value, targets):
            return target.id, stmts[0].value
        return None

    body = assigned(node.body)
    orelse = assigned(node.orelse)
    if body and orelse and body[0] == orelse[0]:
        return body[0]
    return ""


def _role_ifexp(expr: ast.AST, targets: set[str]) -> bool:
    return (
        isinstance(expr, ast.IfExp)
        and _loads(expr.test, targets)
        and _is_foreign_prefix(expr.body, targets)
        and _is_foreign_prefix(expr.orelse, targets)
    )


def _contains_role_ifexp(expr: ast.AST, targets: set[str]) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.IfExp) and _role_ifexp(node, targets):
            return True
    return False


def _is_foreign_prefix(expr: ast.AST, targets: set[str]) -> bool:
    """A prefix string that does not read the loop item (a role marker, not the item's text)."""
    if _loads(expr, targets):
        return False
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, str)
    return isinstance(expr, (ast.Name, ast.Attribute))


def _added_to_name(node: ast.AST, name: str) -> ast.AST | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"append", "extend"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == name
        and node.args
    ):
        return node.args[0]
    if (
        isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
        and isinstance(node.op, ast.Add)
    ):
        return node.value
    return None


def _is_join_call(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "join"


def _target_names(target: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}


def _loads(expr: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names for node in ast.walk(expr)
    )


def _follow_expr(expr: ast.AST, func: ast.AST | None, line: int, depth: int = 0) -> ast.AST:
    if depth > 4 or not isinstance(expr, ast.Name) or func is None:
        return expr
    value = _assigned_value(func, expr.id, line)
    if not isinstance(value, ast.expr):
        return expr
    return _follow_expr(value, func, getattr(value, "lineno", line), depth + 1)


def _assigned_value(func: ast.AST | None, name: str, line: int) -> ast.AST | None:
    if func is None:
        return None
    value = _last_assignment(func, name, line)
    if isinstance(value, ast.AnnAssign):
        return value.value
    return value if isinstance(value, ast.expr) else None


def _return_exprs(fn: ast.AST) -> list[ast.AST]:
    return [
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and node.value is not None
    ]


def _functions_named(tree: ast.AST, name: str) -> list[ast.AST]:
    if not name:
        return []
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""
