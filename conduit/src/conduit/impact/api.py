"""Diff the public Python API of two wheels (or unpacked trees)."""

from __future__ import annotations

import ast
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from conduit.impact.model import ApiChange

_SKIP_DIRS = {"tests", "test", "testing", "__pycache__"}


@dataclass(frozen=True)
class Symbol:
    qualname: str
    params: tuple[str, ...]
    kwonly: tuple[str, ...]
    required: tuple[str, ...]
    deprecated: bool


def diff_wheels(old: Path, new: Path) -> list[ApiChange]:
    """Removed, renamed, and signature-changed public callables, plus deprecations."""
    old_root = _materialize(old)
    new_root = _materialize(new)
    before_all = load_api(old_root, include_private=True)
    after_all = load_api(new_root, include_private=True)
    old_index = _index_tree(old_root)
    new_index = _index_tree(new_root)
    aliases = _public_aliases(old_index, set(before_all))
    new_aliases = _public_aliases(new_index, set(after_all))
    before = _public_symbols(before_all, aliases)
    after = _public_symbols(after_all, new_aliases)
    new_public = set(after) | {name for names in new_aliases.values() for name in names}
    new_public |= _module_values(new_index)
    # A re-export that still exists under the same public name was moved, not removed.
    aliases = {
        key: tuple(name for name in names if name not in new_public)
        for key, names in aliases.items()
    }
    old_returns = _return_kinds(old_index)
    new_returns = _return_kinds(new_index)
    new_models = any(kind == "model" for kind in _class_kinds(new_index).values())
    changes: list[ApiChange] = []
    for name in sorted(before):
        sym = before[name]
        other = after.get(name)
        if other is None:
            # The defining path moved, but the same public name is still exported
            # (a compatibility star-import, ``from package.sync.collection import *``).
            if name in new_public:
                continue
            changes.append(
                _alias(
                    ApiChange(
                        kind="removed",
                        symbol=name,
                        old=name,
                        new="",
                        detail=f"{_short(name)} removed",
                        confidence="high",
                        source="wheel",
                    ),
                    aliases,
                )
            )
        else:
            if other.deprecated and not sym.deprecated:
                changes.append(
                    _alias(
                        ApiChange(
                            kind="deprecated",
                            symbol=name,
                            old=name,
                            new=name,
                            detail=f"{_short(name)} deprecated",
                            confidence="low",
                            source="wheel",
                        ),
                        aliases,
                    )
                )
            changes.extend(_alias(item, aliases) for item in _signature(sym, other))
        if _response_shape_changed(name, old_returns, new_returns, other is None, new_models):
            changes.append(
                _alias(
                    ApiChange(
                        kind="response_shape",
                        symbol=name,
                        old="dict",
                        new="model",
                        detail=(
                            f"{_short(name)} return changed from a dict-like object to a typed object"
                        ),
                        confidence="medium",
                        source="wheel",
                    ),
                    aliases,
                )
            )
    old_values = _module_values(old_index)
    callable_names = set(before) | set(after)
    for name in sorted(old_values - _module_values(new_index)):
        if name in callable_names or name in new_public:
            continue
        changes.append(
            ApiChange(
                kind="removed",
                symbol=name,
                old=name,
                new="",
                detail=f"{_short(name)} removed",
                confidence="high",
                source="wheel",
            )
        )
    diff = _WheelDiff(changes)
    diff.return_classes = _return_classes(old_index)
    diff.methods = _method_owners(old_index)
    diff.surviving = new_public
    return diff


class _WheelDiff(list):
    """A wheel diff plus the facts the matcher needs to follow returns.

    Callers that only iterate the changes keep working. ``return_classes``
    maps a function qualname to the class it constructs. ``methods`` maps
    ``Class.method`` to the base that defines it when the class itself does
    not. ``surviving`` is every public name the new wheel still exports.
    """

    return_classes: dict[str, set[str]]
    methods: dict[str, str]
    surviving: set[str]

    def __init__(self, changes: list[ApiChange]) -> None:
        super().__init__(changes)
        self.return_classes = {}
        self.methods = {}
        self.surviving = set()


def load_api(path: Path, *, include_private: bool = False) -> dict[str, Symbol]:
    root = _materialize(path)
    found: dict[str, Symbol] = {}
    for py in sorted(root.rglob("*.py")):
        if not py.is_file() or _skipped(py, root):
            continue
        module = _module_name(py, root)
        leaf = module.split(".")[-1] if module else ""
        if not module or leaf.startswith("_") and py.name != "__init__.py" and not include_private:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
                found[f"{module}.{node.name}"] = _symbol(f"{module}.{node.name}", node)
            elif isinstance(node, ast.ClassDef) and _public(node.name):
                found[f"{module}.{node.name}"] = _symbol(f"{module}.{node.name}", node)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(child.name):
                        qual = f"{module}.{node.name}.{child.name}"
                        found[qual] = _symbol(qual, child)
    return found


def _signature(old: Symbol, new: Symbol) -> list[ApiChange]:
    old_names = set(old.params) | set(old.kwonly)
    new_names = set(new.params) | set(new.kwonly)
    changes: list[ApiChange] = []
    consumed_old: set[str] = set()
    consumed_new: set[str] = set()
    for index, name in enumerate(old.params):
        if name in new_names:
            continue
        replacement = ""
        if index < len(new.params) and new.params[index] not in old_names:
            replacement = new.params[index]
        if replacement:
            consumed_old.add(name)
            consumed_new.add(replacement)
            changes.append(
                ApiChange(
                    kind="param_renamed",
                    symbol=old.qualname,
                    old=name,
                    new=replacement,
                    detail=(
                        f"parameter {name} renamed to {replacement} on {_short(old.qualname)}"
                    ),
                    confidence="medium",
                    source="wheel",
                )
            )
    for name in list(old.params) + list(old.kwonly):
        if name in consumed_old or name in new_names:
            continue
        changes.append(
            ApiChange(
                kind="param_removed",
                symbol=old.qualname,
                old=name,
                new="",
                detail=f"parameter {name} removed from {_short(old.qualname)}",
                confidence="high",
                source="wheel",
            )
        )
    for name in new.required:
        if name in old_names or name in consumed_new:
            continue
        changes.append(
            ApiChange(
                kind="param_added",
                symbol=old.qualname,
                old="",
                new=name,
                detail=f"required parameter {name} added on {_short(old.qualname)}",
                confidence="low",
                source="wheel",
            )
        )
    return changes


def _symbol(qualname: str, node: ast.AST) -> Symbol:
    if isinstance(node, ast.ClassDef):
        init = next(
            (
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__"
            ),
            None,
        )
        params, kwonly, required = _params(init) if init is not None else ((), (), ())
        return Symbol(qualname, params, kwonly, required, _deprecated(node) or (init is not None and _deprecated(init)))
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    params, kwonly, required = _params(node)
    return Symbol(qualname, params, kwonly, required, _deprecated(node))


def _params(node: ast.AST | None) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (), (), ()
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults = list(args.defaults)
    first_default = len(positional) - len(defaults)
    names: list[str] = []
    required: list[str] = []
    for index, arg in enumerate(positional):
        if arg.arg in {"self", "cls"}:
            continue
        names.append(arg.arg)
        if index < first_default:
            required.append(arg.arg)
    kwonly: list[str] = []
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        kwonly.append(arg.arg)
        if default is None:
            required.append(arg.arg)
    return tuple(names), tuple(kwonly), tuple(required)


def _deprecated(node: ast.AST) -> bool:
    for dec in getattr(node, "decorator_list", []) or []:
        if "deprecat" in _callable_name(dec).lower():
            return True
    doc = ast.get_docstring(node) or ""
    if "deprecat" in doc.lower():
        return True
    body = getattr(node, "body", []) or []
    for child in body:
        if not isinstance(child, ast.Expr) or not isinstance(child.value, ast.Call):
            continue
        func = child.value.func
        label = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if label != "warn":
            continue
        blob = ast.dump(child.value)
        if "Deprecation" in blob or "deprecat" in blob.lower():
            return True
    return False


def _callable_name(node: ast.AST) -> str:
    cur = node.func if isinstance(node, ast.Call) else node
    if isinstance(cur, ast.Name):
        return cur.id
    if isinstance(cur, ast.Attribute):
        return cur.attr
    return ""


def _public(name: str) -> bool:
    return bool(name) and not name.startswith("_")


def _public_symbols(symbols: dict[str, Symbol], aliases: dict[str, tuple[str, ...]]) -> dict[str, Symbol]:
    """Public modules, plus names re-exported from a public module.

    SDKs often define ``Charge`` in ``stripe/_charge.py`` and export it from
    ``stripe/__init__.py``. The defining module is private; the export is the
    public surface. Private helpers that are not re-exported stay out.
    """
    exported = set(aliases)
    keep: dict[str, Symbol] = {}
    for name, sym in symbols.items():
        private = any(part.startswith("_") for part in name.split("."))
        if private and name not in exported and not any(name.startswith(item + ".") for item in exported):
            continue
        keep[name] = sym
    return keep


def _short(qualname: str) -> str:
    parts = qualname.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return qualname


def _materialize(path: Path) -> Path:
    path = path.resolve()
    if path.is_dir():
        return path
    dest = Path(tempfile.mkdtemp(prefix="conduit-wheel-"))
    with zipfile.ZipFile(path) as archive:
        archive.extractall(dest)
    return dest


def _skipped(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    if any(part.endswith(".dist-info") or part.endswith(".egg-info") for part in parts):
        return True
    return any(part in _SKIP_DIRS or part.startswith("_") and part != "__init__.py" for part in parts[:-1])


def _alias(change: ApiChange, aliases: dict[str, tuple[str, ...]]) -> ApiChange:
    found = aliases.get(change.symbol, ())
    if not found:
        return change
    return ApiChange(
        kind=change.kind,
        symbol=change.symbol,
        old=change.old,
        new=change.new,
        detail=change.detail,
        confidence=change.confidence,
        source=change.source,
        service=change.service,
        aliases=found,
    )


def _response_shape_changed(
    name: str,
    old_returns: dict[str, str],
    new_returns: dict[str, str],
    removed: bool,
    new_models: bool,
) -> bool:
    """Dict subclass (``OpenAIObject``) then pydantic ``BaseModel``."""
    if old_returns.get(name) != "dict":
        return False
    if removed:
        return new_models
    return new_returns.get(name) == "model"


@dataclass(frozen=True)
class _Mod:
    name: str
    package: str
    tree: ast.AST
    bindings: dict[str, str]
    stars: tuple[str, ...]
    defined: dict[str, str]
    classes: dict[str, ast.ClassDef]
    functions: dict[str, ast.AST]
    exported: tuple[str, ...]


def _index_tree(root: Path) -> dict[str, _Mod]:
    """Parse a wheel, including private modules, for re-exports and types."""
    mods: dict[str, _Mod] = {}
    for py in sorted(root.rglob("*.py")):
        if not py.is_file() or _skipped(py, root):
            continue
        module = _module_name(py, root)
        if not module:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        package = module if py.name == "__init__.py" else module.rsplit(".", 1)[0]
        bindings, stars = _bind_imports(module, package, tree)
        defined, classes, functions = _definitions(module, tree)
        exported = _exported_names(tree)
        mods[module] = _Mod(
            name=module,
            package=package,
            tree=tree,
            bindings=bindings,
            stars=stars,
            defined=defined,
            classes=classes,
            functions=functions,
            exported=exported,
        )
    return mods


def _bind_imports(module: str, package: str, tree: ast.AST) -> tuple[dict[str, str], tuple[str, ...]]:
    bindings: dict[str, str] = {}
    stars: list[str] = []
    for node in _module_level(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                bindings[local] = alias.name if alias.asname else alias.name.split(".")[0]
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _abs_module(package, node.level or 0, node.module or "")
        for alias in node.names:
            if alias.name == "*":
                if base:
                    stars.append(base)
                continue
            local = alias.asname or alias.name
            bindings[local] = f"{base}.{alias.name}" if base else alias.name
    return bindings, tuple(stars)


def _definitions(
    module: str, tree: ast.AST
) -> tuple[dict[str, str], dict[str, ast.ClassDef], dict[str, ast.AST]]:
    defined: dict[str, str] = {}
    classes: dict[str, ast.ClassDef] = {}
    functions: dict[str, ast.AST] = {}
    for node in _module_level(tree):
        if isinstance(node, ast.ClassDef) and _public(node.name):
            qual = f"{module}.{node.name}"
            defined[node.name] = qual
            classes[qual] = node
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Dunder methods stay out of the public surface. ``__getitem__``
                # still has a return class, because ``client[name]`` is that call.
                if child.name in {"__init__", "__new__"}:
                    continue
                if child.name.startswith("_") and not (
                    child.name.startswith("__") and child.name.endswith("__")
                ):
                    method = f"{qual}.{child.name}"
                    functions[method] = child
                    continue
                if child.name.startswith("__"):
                    functions[f"{qual}.{child.name}"] = child
                    continue
                method = f"{qual}.{child.name}"
                functions[method] = child
                if _public(child.name):
                    defined[f"{node.name}.{child.name}"] = method
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
            qual = f"{module}.{node.name}"
            defined[node.name] = qual
            functions[qual] = node
    return defined, classes, functions


def _module_values(mods: dict[str, _Mod]) -> set[str]:
    """Public data names assigned at module level (``pkg.api_base = ...``).

    Functions and classes are already in the callable surface. Names that
    start with ``_`` are not part of that surface.
    """
    found: set[str] = set()
    for mod in mods.values():
        defined = set(mod.defined)
        for node in _module_level(mod.tree):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if not _public(target.id) or target.id in defined:
                    continue
                found.add(f"{mod.name}.{target.id}")
    return found


def _exported_names(tree: ast.AST) -> tuple[str, ...]:
    for node in _module_level(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        names = [
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        return tuple(names)
    return ()


def _module_level(tree: ast.AST):
    """Module statements, including those nested in ``if`` but not in defs."""
    stack = list(getattr(tree, "body", []) or [])
    while stack:
        node = stack.pop(0)
        if isinstance(node, ast.If):
            stack = list(node.body) + list(node.orelse) + stack
            continue
        yield node


def _abs_module(package: str, level: int, module: str) -> str:
    if level <= 0:
        return module
    parts = [part for part in package.split(".") if part]
    drop = level - 1
    if drop:
        parts = parts[:-drop] if drop <= len(parts) else []
    if module:
        parts.extend(part for part in module.split(".") if part)
    return ".".join(parts)


def _public_aliases(mods: dict[str, _Mod], symbols: set[str]) -> dict[str, tuple[str, ...]]:
    """Map a defining qualname to public names from package and submodule re-exports."""
    found: dict[str, set[str]] = {}

    def lookup(module: str, name: str, stack: set[tuple[str, str]]) -> str | None:
        key = (module, name)
        if key in stack or module not in mods:
            return None
        stack.add(key)
        mod = mods[module]
        if name in mod.defined:
            return mod.defined[name]
        target = mod.bindings.get(name)
        if target and "." in target:
            src_mod, _, src_name = target.rpartition(".")
            resolved = lookup(src_mod, src_name, stack)
            if resolved:
                return resolved
        for source in mod.stars:
            resolved = lookup(source, name, stack)
            if resolved:
                return resolved
        return None

    for module, mod in mods.items():
        names = set(mod.bindings) | set(mod.defined)
        for source in mod.stars:
            src = mods.get(source)
            if src is None:
                continue
            names.update(src.exported or set(src.defined) | set(src.bindings))
        for name in names:
            if not name or name.startswith("_") or "." in name:
                continue
            target = lookup(module, name, set())
            if not target:
                continue
            public = f"{module}.{name}"
            if public != target:
                found.setdefault(target, set()).add(public)
            if target in mod.classes or any(item.startswith(target + ".") for item in symbols):
                prefix = target + "."
                for symbol in symbols:
                    if not symbol.startswith(prefix):
                        continue
                    alias = public + "." + symbol[len(prefix) :]
                    if alias != symbol:
                        found.setdefault(symbol, set()).add(alias)
    return {key: tuple(sorted(value)) for key, value in found.items()}


def _return_classes(mods: dict[str, _Mod]) -> dict[str, set[str]]:
    """Class a function returns, from its annotation or ``return Class(...)``.

    A later call can use this to turn ``client.messages.create`` into
    ``Messages.create``, and ``db[name].insert`` into ``Collection.insert``.
    """
    classes = {qual for mod in mods.values() for qual in mod.classes}
    inverse: dict[str, str] = {}
    symbols = {
        symbol
        for mod in mods.values()
        for symbol in (*mod.functions, *mod.classes)
    }
    aliases = _public_aliases(mods, symbols)
    for defining, names in aliases.items():
        if defining in classes:
            for name in names:
                inverse.setdefault(name, defining)
    direct: dict[str, set[str]] = {}
    pending: dict[str, set[str]] = {}
    for mod in mods.values():
        for qual, node in mod.functions.items():
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            owner, _method = _owner_method(qual)
            local = _function_bindings(mod, node)
            binds = _name_binds(node)
            found: set[str] = set()
            calls: set[str] = set()
            if node.returns is not None:
                found |= _annotation_classes(mod, node.returns, classes, inverse, local)
            for expr in _return_exprs(node):
                klasses, called = _expr_classes(
                    mod, expr, binds, owner, classes, inverse, local, depth=0
                )
                found |= klasses
                calls |= called
            if found:
                direct[qual] = found
            if calls:
                pending[qual] = calls
    resolved = {key: set(value) for key, value in direct.items()}
    for _ in range(len(pending) + 2):
        changed = False
        for qual, called in pending.items():
            found = set(resolved.get(qual, ()))
            for target in called:
                inherited = set(resolved.get(target, ()))
                if not inherited:
                    owner, method = _owner_method(target)
                    inherited = _base_return_classes(owner, method, mods, resolved)
                if inherited - found:
                    found |= inherited
                    changed = True
            if found and found != resolved.get(qual):
                resolved[qual] = found
                changed = True
        if not changed:
            break
    found = {key: value for key, value in resolved.items() if value}
    # ``pkg.Client.method`` is the name a caller writes; the body lives on
    # ``pkg._mod.Client.method``. Both name the same return.
    for qual, klasses in list(found.items()):
        for alias in aliases.get(qual, ()):
            found.setdefault(alias, set()).update(klasses)
    return found


def _function_bindings(mod: _Mod, node: ast.AST) -> dict[str, str]:
    """Imports inside the function (``from .resources.messages import Messages``)."""
    found: dict[str, str] = {}
    for child in _walk_function(node):
        if isinstance(child, ast.ImportFrom):
            base = _abs_module(mod.package, child.level or 0, child.module or "")
            for alias in child.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                found[local] = f"{base}.{alias.name}" if base else alias.name
        elif isinstance(child, ast.Import):
            for alias in child.names:
                local = alias.asname or alias.name.split(".")[0]
                found[local] = alias.name if alias.asname else alias.name.split(".")[0]
    return found


def _annotation_classes(
    mod: _Mod,
    node: ast.AST,
    classes: set[str],
    inverse: dict[str, str],
    local: dict[str, str],
) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        resolved = _class_named(mod, node.value, classes, inverse, local)
        return {resolved} if resolved else set()
    if isinstance(node, ast.Name):
        dotted = local.get(node.id) or _dotted(mod, node)
        resolved = _class_named(mod, dotted, classes, inverse, local)
        return {resolved} if resolved else set()
    if isinstance(node, ast.Attribute):
        resolved = _class_named(mod, _dotted(mod, node), classes, inverse, local)
        return {resolved} if resolved else set()
    if isinstance(node, ast.Subscript):
        # Optional[Engine] / list[Engine]: the parameter, not the container.
        slice_node = node.slice
        return _annotation_classes(mod, slice_node, classes, inverse, local)
    if isinstance(node, ast.BinOp):
        return _annotation_classes(mod, node.left, classes, inverse, local) | _annotation_classes(
            mod, node.right, classes, inverse, local
        )
    return set()


def _class_named(
    mod: _Mod,
    dotted: str,
    classes: set[str],
    inverse: dict[str, str],
    local: dict[str, str],
) -> str:
    if not dotted:
        return ""
    if dotted in local:
        dotted = local[dotted]
    if dotted in classes:
        return dotted
    if inverse.get(dotted) in classes:
        return inverse[dotted]
    tail = dotted.split(".")[-1]
    for candidate in (mod.defined.get(tail, ""), mod.bindings.get(tail, ""), local.get(tail, "")):
        if candidate in classes:
            return candidate
        if inverse.get(candidate) in classes:
            return inverse[candidate]
    return ""


def _expr_classes(
    mod: _Mod,
    expr: ast.AST | None,
    binds: dict[str, list[tuple[ast.AST, int | None]]],
    owner: str,
    classes: set[str],
    inverse: dict[str, str],
    local: dict[str, str],
    depth: int,
) -> tuple[set[str], set[str]]:
    """``(classes, called function qualnames)`` for one return expression."""
    if expr is None or depth > 8:
        return set(), set()
    if isinstance(expr, ast.Await):
        return _expr_classes(mod, expr.value, binds, owner, classes, inverse, local, depth + 1)
    if isinstance(expr, ast.Name):
        if expr.id in {"self", "cls"} and owner in classes:
            return {owner}, set()
        if expr.id in local:
            named = _class_named(mod, local[expr.id], classes, inverse, local)
            return ({named} if named else set()), set()
        found: set[str] = set()
        called: set[str] = set()
        for value, index in binds.get(expr.id, []):
            if index is None:
                klasses, more = _expr_classes(
                    mod, value, binds, owner, classes, inverse, local, depth + 1
                )
                found |= klasses
                called |= more
        if found or called:
            return found, called
        named = _class_named(mod, _dotted(mod, expr), classes, inverse, local)
        return ({named} if named else set()), set()
    if isinstance(expr, ast.Attribute):
        named = _class_named(mod, _dotted(mod, expr), classes, inverse, local)
        return ({named} if named else set()), set()
    if isinstance(expr, ast.Call):
        default = _mapping_default(expr)
        if default is not None:
            return _expr_classes(
                mod, default, binds, owner, classes, inverse, local, depth + 1
            )
        constructed = _class_named(mod, _dotted(mod, expr.func), classes, inverse, local)
        if not constructed and isinstance(expr.func, ast.Name) and expr.func.id in {"cls", "self"}:
            constructed = owner if owner in classes else ""
        if constructed:
            return {constructed}, set()
        if isinstance(expr.func, ast.Name) and expr.func.id not in {"self", "cls"}:
            klasses: set[str] = set()
            called: set[str] = set()
            for value, _index in binds.get(expr.func.id, []):
                more, more_calls = _expr_classes(
                    mod, value, binds, owner, classes, inverse, local, depth + 1
                )
                klasses |= more
                called |= more_calls
            if klasses or called:
                return klasses, called
        called_name = _called_qual(mod, expr.func, owner, local, classes, inverse)
        return set(), ({called_name} if called_name else set())
    if isinstance(expr, ast.IfExp):
        left, left_calls = _expr_classes(
            mod, expr.body, binds, owner, classes, inverse, local, depth + 1
        )
        right, right_calls = _expr_classes(
            mod, expr.orelse, binds, owner, classes, inverse, local, depth + 1
        )
        return left | right, left_calls | right_calls
    return set(), set()


def _called_qual(
    mod: _Mod,
    func: ast.AST,
    owner: str,
    local: dict[str, str],
    classes: set[str],
    inverse: dict[str, str],
) -> str:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}:
        return f"{owner}.{func.attr}" if owner else ""
    if isinstance(func, ast.Name):
        return local.get(func.id) or mod.defined.get(func.id) or mod.bindings.get(func.id, "")
    if isinstance(func, ast.Attribute):
        dotted = _dotted(mod, func)
        receiver = _class_named(mod, _dotted(mod, func.value), classes, inverse, local)
        if receiver:
            return f"{receiver}.{func.attr}"
        return dotted
    return ""


def _base_return_classes(
    owner: str,
    method: str,
    mods: dict[str, _Mod],
    resolved: dict[str, set[str]],
) -> set[str]:
    if not owner or not method:
        return set()
    bases: dict[str, tuple[str, ...]] = {}
    for mod in mods.values():
        for qual, node in mod.classes.items():
            bases[qual] = tuple(_base_name(mod, base) for base in node.bases)
    queue = list(bases.get(owner, ()))
    seen = {owner}
    while queue:
        base = queue.pop(0)
        if not base or base in seen:
            continue
        seen.add(base)
        found = resolved.get(f"{base}.{method}")
        if found:
            return set(found)
        queue.extend(bases.get(base, ()))
    return set()


def _class_kinds(mods: dict[str, _Mod]) -> dict[str, str]:
    bases: dict[str, tuple[str, ...]] = {}
    for mod in mods.values():
        for qual, node in mod.classes.items():
            bases[qual] = tuple(_base_name(mod, base) for base in node.bases)
    kinds: dict[str, str] = {}
    pending = set(bases)
    while pending:
        progressed = False
        for qual in list(pending):
            kind = ""
            waiting = False
            for base in bases[qual]:
                if not base:
                    continue
                tail = base.split(".")[-1]
                if tail == "dict":
                    kind = "dict"
                    continue
                if _is_model_base(base):
                    kind = "model"
                    continue
                if base in bases and base not in kinds and base != qual:
                    waiting = True
                    continue
                parent = kinds.get(base, "")
                if parent == "dict" and kind != "model":
                    kind = "dict"
                elif parent == "model":
                    kind = "model"
            if waiting and not kind:
                continue
            kinds[qual] = kind
            pending.discard(qual)
            progressed = True
        if not progressed:
            for qual in pending:
                kinds[qual] = ""
            break
    return kinds


def _is_model_base(base: str) -> bool:
    tail = base.split(".")[-1]
    if tail != "BaseModel":
        return False
    lowered = base.lower()
    return "pydantic" in lowered or "_models" in lowered or lowered.endswith(".models.basemodel") or base == "pydantic.BaseModel"


def _base_name(mod: _Mod, node: ast.AST) -> str:
    """``class Child(Base["T"])`` still inherits from ``Base``."""
    while isinstance(node, ast.Subscript):
        node = node.value
    return _dotted(mod, node)


def _method_owners(mods: dict[str, _Mod]) -> dict[str, str]:
    """``Child.method`` → ``Base.method`` when the child does not define it."""
    methods: dict[str, set[str]] = {}
    bases: dict[str, tuple[str, ...]] = {}
    for mod in mods.values():
        for qual, node in mod.classes.items():
            bases[qual] = tuple(_base_name(mod, base) for base in node.bases)
        for qual in mod.functions:
            owner, method = _owner_method(qual)
            if owner and method:
                methods.setdefault(owner, set()).add(method)
    found: dict[str, str] = {}
    for owner in bases:
        own = methods.get(owner, set())
        seen_methods: set[str] = set()
        walked = {owner}
        queue = list(bases.get(owner, ()))
        while queue:
            base = queue.pop(0)
            if not base or base in walked:
                continue
            walked.add(base)
            for method in methods.get(base, ()):
                if method in own or method in seen_methods:
                    continue
                seen_methods.add(method)
                found[f"{owner}.{method}"] = f"{base}.{method}"
            queue.extend(bases.get(base, ()))
    return found


def _mapping_default(expr: ast.Call) -> ast.AST | None:
    """``kwargs.pop(key, Factory)`` may be the value held in that name.

    A method call on ``self`` or ``cls`` stays a call, so ``client.get`` is
    not replaced by its default argument.
    """
    func = expr.func
    if not isinstance(func, ast.Attribute) or func.attr not in {"get", "pop"}:
        return None
    if len(expr.args) < 2 or not isinstance(func.value, ast.Name):
        return None
    if func.value.id in {"self", "cls"}:
        return None
    return expr.args[1]


def _dotted(mod: _Mod, node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return mod.bindings.get(node.id) or (mod.defined.get(node.id) or node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(mod, node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _return_kinds(mods: dict[str, _Mod]) -> dict[str, str]:
    """``dict`` for an OpenAIObject-style return, ``model`` for a BaseModel."""
    classes = _class_kinds(mods)
    hints: dict[str, set[str]] = {}
    supers: dict[str, tuple[str, str]] = {}
    for mod in mods.values():
        for qual, node in mod.functions.items():
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hints[qual] = _return_hints(mod, node, classes, qual)
            owner, method = _owner_method(qual)
            delegated = _super_method(node)
            if owner and delegated:
                supers[qual] = (owner, delegated)
    kinds: dict[str, str] = {qual: "" for qual in hints}
    bases = {
        qual: tuple(_base_name(mod, base) for base in node.bases)
        for mod in mods.values()
        for qual, node in mod.classes.items()
    }
    for _ in range(len(hints) + 2):
        changed = False
        for qual, items in hints.items():
            kind = kinds[qual]
            for item in items:
                if item in {"dict", "model"}:
                    kind = _prefer_kind(kind, item)
                elif item.startswith("fn:"):
                    target = item[3:]
                    inherited = kinds.get(target, "")
                    if not inherited:
                        base_owner, base_method = _owner_method(target)
                        inherited = _base_method_kind(base_owner, base_method, bases, kinds)
                    kind = _prefer_kind(kind, inherited)
            owner_method = supers.get(qual)
            if owner_method:
                owner, method = owner_method
                kind = _prefer_kind(kind, _base_method_kind(owner, method, bases, kinds))
            if kind != kinds[qual]:
                kinds[qual] = kind
                changed = True
        if not changed:
            break
    return {qual: kind for qual, kind in kinds.items() if kind}


def _prefer_kind(current: str, new: str) -> str:
    if not new:
        return current
    if new == "model" or current == "model":
        return "model"
    if new == "dict":
        return "dict"
    return current


def _owner_method(qual: str) -> tuple[str, str]:
    parts = qual.split(".")
    if len(parts) < 3:
        return "", ""
    return ".".join(parts[:-1]), parts[-1]


def _base_method_kind(
    owner: str,
    method: str,
    bases: dict[str, tuple[str, ...]],
    kinds: dict[str, str],
) -> str:
    seen: set[str] = set()
    queue = list(bases.get(owner, ()))
    while queue:
        base = queue.pop(0)
        if not base or base in seen:
            continue
        seen.add(base)
        kind = kinds.get(f"{base}.{method}", "")
        if kind:
            return kind
        queue.extend(bases.get(base, ()))
    return ""


def _return_hints(mod: _Mod, node: ast.AST, classes: dict[str, str], qual: str) -> set[str]:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    _expr_kinds.seen = set()
    found: set[str] = set()
    if node.returns is not None:
        found |= _expr_kinds(mod, node.returns, {}, classes)
    assigns = _local_assigns(node)
    for expr in _return_exprs(node):
        found |= _expr_kinds(mod, expr, assigns, classes)
    found |= _instance_call_hints(mod, node, qual)
    return found


def _local_assigns(node: ast.AST) -> dict[str, list[ast.AST]]:
    found: dict[str, list[ast.AST]] = {}
    for child in _walk_function(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(child.value)
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.value is not None:
            found.setdefault(child.target.id, []).append(child.value)
    return found


def _return_exprs(node: ast.AST) -> list[ast.expr]:
    found: list[ast.expr] = []
    for child in _walk_function(node):
        if isinstance(child, ast.Return) and child.value is not None:
            found.append(child.value)
    return found


def _walk_function(node: ast.AST):
    """Statements in this function, not nested functions or classes."""
    stack = list(getattr(node, "body", []) or [])
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        stack.extend(list(ast.iter_child_nodes(child)))


def _expr_kinds(
    mod: _Mod,
    node: ast.AST | None,
    assigns: dict[str, list[ast.AST]],
    classes: dict[str, str],
) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Await):
        return _expr_kinds(mod, node.value, assigns, classes)
    if isinstance(node, ast.Dict):
        return {"dict"}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        found: set[str] = set()
        for elt in node.elts:
            found |= _expr_kinds(mod, elt, assigns, classes)
        return found
    if isinstance(node, ast.IfExp):
        return _expr_kinds(mod, node.body, assigns, classes) | _expr_kinds(mod, node.orelse, assigns, classes)
    if isinstance(node, ast.Subscript):
        return _expr_kinds(mod, node.value, assigns, classes)
    if isinstance(node, ast.Name):
        if node.id in assigns and node.id not in _expr_kinds.seen:
            _expr_kinds.seen.add(node.id)
            try:
                found: set[str] = set()
                for expr in assigns[node.id]:
                    found |= _expr_kinds(mod, expr, assigns, classes)
                return found
            finally:
                _expr_kinds.seen.discard(node.id)
        return _named_kind(mod, _dotted(mod, node), classes)
    if isinstance(node, ast.Attribute):
        return _named_kind(mod, _dotted(mod, node), classes)
    if isinstance(node, ast.Call):
        found = _expr_kinds(mod, node.func, assigns, classes)
        # ``klass = OpenAIObject; return klass.construct_from(...)``
        if isinstance(node.func, ast.Attribute):
            found |= _expr_kinds(mod, node.func.value, assigns, classes)
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            dotted = _dotted(mod, arg)
            if dotted:
                found |= _named_kind(mod, dotted, classes)
        return found
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _expr_kinds(mod, node.left, assigns, classes) | _expr_kinds(mod, node.right, assigns, classes)
    return set()


def _named_kind(mod: _Mod, dotted: str, classes: dict[str, str]) -> set[str]:
    if not dotted:
        return set()
    tail = dotted.split(".")[-1]
    if tail in {"dict", "Dict", "OrderedDict", "defaultdict"}:
        return {"dict"}
    kind = classes.get(dotted, "")
    if kind:
        return {kind}
    resolved = mod.defined.get(tail) if "." not in dotted else ""
    if resolved and classes.get(resolved):
        return {classes[resolved]}
    if dotted.split(".")[-1][:1].isupper() or "." in dotted:
        return {f"fn:{dotted}"}
    return set()


def _super_method(node: ast.AST) -> str:
    """Method name when the return is ``super().method()`` or a name bound to that call."""
    bound: dict[str, str] = {}
    for child in _walk_function(node):
        value = None
        target = None
        if isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
            target = child.targets[0].id
            value = child.value
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            target = child.target.id
            value = child.value
        method = _super_attr(value)
        if target and method:
            bound[target] = method
    for expr in _return_exprs(node):
        inner = expr.value if isinstance(expr, ast.Await) else expr
        method = _super_attr(inner)
        if method:
            return method
        if isinstance(inner, ast.Name) and inner.id in bound:
            return bound[inner.id]
    return ""


def _super_attr(expr: ast.AST | None) -> str:
    if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
        return ""
    receiver = expr.func.value
    if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name) and receiver.func.id == "super":
        return expr.func.attr
    return ""


def _instance_call_hints(mod: _Mod, node: ast.AST, qual: str) -> set[str]:
    """``fn:Class.method`` when a return calls a method on an instance of ``Class``.

    The class comes from ``cls()`` / ``ClassName()`` and from tuple slots of
    another method on the same class. The method body is classified on its
    own; this only links the call.
    """
    owner, _method = _owner_method(qual)
    if not owner:
        return set()
    binds = _name_binds(node)
    found: set[str] = set()
    for expr in _return_exprs(node):
        found |= _methods_on_return(mod, expr, binds, owner, depth=0)
    return found


def _methods_on_return(
    mod: _Mod,
    expr: ast.AST,
    binds: dict[str, list[tuple[ast.AST, int | None]]],
    owner: str,
    depth: int,
) -> set[str]:
    if depth > 6:
        return set()
    inner = expr.value if isinstance(expr, ast.Await) else expr
    if isinstance(inner, ast.Name):
        found: set[str] = set()
        for value, index in binds.get(inner.id, []):
            if index is None:
                found |= _methods_on_return(mod, value, binds, owner, depth + 1)
        return found
    if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
        return set()
    if _super_attr(inner):
        return set()
    klasses = _classes_of(mod, inner.func.value, binds, owner, depth)
    return {f"fn:{klass}.{inner.func.attr}" for klass in klasses}


def _classes_of(
    mod: _Mod,
    expr: ast.AST | None,
    binds: dict[str, list[tuple[ast.AST, int | None]]],
    owner: str,
    depth: int,
) -> set[str]:
    if expr is None or depth > 6:
        return set()
    inner = expr.value if isinstance(expr, ast.Await) else expr
    if isinstance(inner, ast.Name):
        if inner.id in {"self", "cls"}:
            return {owner}
        found: set[str] = set()
        for value, index in binds.get(inner.id, []):
            if index is None:
                found |= _classes_of(mod, value, binds, owner, depth + 1)
            else:
                found |= _slot_classes(mod, value, index, owner, depth + 1)
        return found
    if isinstance(inner, ast.Call):
        constructed = _constructed_class(mod, inner.func, owner)
        if constructed:
            return {constructed}
    return set()


def _slot_classes(mod: _Mod, expr: ast.AST, index: int, owner: str, depth: int) -> set[str]:
    """Class of one element of a tuple returned by a call."""
    if depth > 6 or not isinstance(expr, ast.Call):
        return set()
    qual = _called_method(mod, expr.func, owner)
    target = mod.functions.get(qual)
    if target is None or not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    target_owner, _name = _owner_method(qual)
    binds = _name_binds(target)
    found: set[str] = set()
    for item in _return_exprs(target):
        inner = item.value if isinstance(item, ast.Await) else item
        if isinstance(inner, (ast.Tuple, ast.List)):
            if index < len(inner.elts):
                found |= _classes_of(mod, inner.elts[index], binds, target_owner or owner, depth + 1)
        elif index == 0:
            found |= _classes_of(mod, inner, binds, target_owner or owner, depth + 1)
    return found


def _called_method(mod: _Mod, func: ast.AST, owner: str) -> str:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}:
        return f"{owner}.{func.attr}"
    if isinstance(func, ast.Name):
        return mod.defined.get(func.id, "")
    if isinstance(func, ast.Attribute):
        dotted = _dotted(mod, func)
        return dotted
    return ""


def _constructed_class(mod: _Mod, func: ast.AST, owner: str) -> str:
    if isinstance(func, ast.Name) and func.id in {"cls", "self"}:
        return owner
    if isinstance(func, ast.Name):
        qual = mod.defined.get(func.id) or ""
        if qual in mod.classes:
            return qual
        bound = mod.bindings.get(func.id, "")
        return bound if bound in mod.classes else ""
    if isinstance(func, ast.Attribute):
        dotted = _dotted(mod, func)
        return dotted if dotted in mod.classes else ""
    return ""


def _name_binds(node: ast.AST) -> dict[str, list[tuple[ast.AST, int | None]]]:
    found: dict[str, list[tuple[ast.AST, int | None]]] = {}
    for child in _walk_function(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                _bind_target(found, target, child.value, None)
        elif isinstance(child, ast.AnnAssign) and child.value is not None:
            _bind_target(found, child.target, child.value, None)
    return found


def _bind_target(
    found: dict[str, list[tuple[ast.AST, int | None]]],
    target: ast.AST,
    value: ast.AST,
    index: int | None,
) -> None:
    if isinstance(target, ast.Name):
        found.setdefault(target.id, []).append((value, index))
        return
    if isinstance(target, (ast.Tuple, ast.List)) and index is None:
        if isinstance(value, (ast.Tuple, ast.List)):
            for slot, elt in enumerate(target.elts):
                if slot < len(value.elts):
                    _bind_target(found, elt, value.elts[slot], None)
        else:
            for slot, elt in enumerate(target.elts):
                _bind_target(found, elt, value, slot)


def _module_name(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ""
    if rel.name == "__init__.py":
        rel = rel.parent
    else:
        rel = rel.with_suffix("")
    parts = [part for part in rel.parts if part]
    if not parts:
        return ""
    return ".".join(parts)
