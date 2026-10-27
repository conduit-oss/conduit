"""Declared types: a value whose class is certain, passed where another class is declared.

A value's class is certain when it is read from declarations only: a parameter
annotation, a constructor call, a declared return type, or an attribute whose
class declares its type (an annotated field, a ``@property`` return, a
``TypedDict`` key). Each name resolves through the module's own definitions
and imports to one class definition, in this repo's code or the new release's
code, so two classes that share a bare name in different modules stay two
classes. Anything that does not resolve this way is unknown and never reported.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conduit.apply.signatures import _absolute, _is_package

_MAX_DEPTH = 8
_UNION = {"Optional", "Union"}
_WRAPS = {"Annotated", "Required", "NotRequired", "ReadOnly", "Final"}
_PROPERTY = {"property", "cached_property"}
_SKIP_DIRS = {"tests", "test", ".venv", "venv", "node_modules"}

Klass = tuple[str, str, ast.ClassDef]  # (module, qualified name in module, definition)


def module_name(rel: str) -> str:
    """``pkg/sub/mod.py`` -> ``pkg.sub.mod``; a package's ``__init__.py`` -> the package."""
    dotted = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")
    return dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted


def repo_modules(root: Path, current: str, tree: ast.Module) -> dict[str, ast.Module]:
    """This repo's modules by dotted name, with ``current`` read from ``tree``."""
    out: dict[str, ast.Module] = {}
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.split("/")[:-1]):
            continue
        if rel == current:
            parsed = tree
        else:
            try:
                parsed = ast.parse(file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
                continue
        for name in _names_for(rel):
            out[name] = parsed
    return out


def _names_for(rel: str) -> list[str]:
    name = module_name(rel)
    return [name, name[len("src."):]] if name.startswith("src.") else [name]


class Namespace:
    """Modules of this repo and of the new release; ``local`` names the repo's."""

    def __init__(self, repo: dict[str, ast.Module], release: dict[str, ast.Module]) -> None:
        self.modules = {**repo, **release}
        self.local = set(repo) - set(release)

    def is_local(self, module: str) -> bool:
        return module in self.local

    # --- names -------------------------------------------------------------------

    def resolve(self, module: str, expr: ast.AST, depth: int = 0):
        """What ``expr`` (a name or dotted name) means in ``module``.

        ``("class", Klass)``, ``("function", module, node, owner)``,
        ``("module", name)``, ``("alias", module, value)`` or ``None``."""
        if depth > _MAX_DEPTH:
            return None
        if isinstance(expr, ast.Name):
            return self._lookup(module, expr.id, depth)
        if isinstance(expr, ast.Attribute):
            base = self.resolve(module, expr.value, depth + 1)
            if base is None:
                return None
            if base[0] == "module":
                return self._lookup(base[1], expr.attr, depth + 1)
            if base[0] == "class":
                return self._class_member(base[1], expr.attr)
        return None

    def _lookup(self, module: str, name: str, depth: int):
        if depth > _MAX_DEPTH:
            return None
        tree = self.modules.get(module)
        if tree is None:
            sub = f"{module}.{name}"
            return ("module", sub) if sub in self.modules else None
        bindings = [stmt for stmt in _top_level(tree.body) if _binds(stmt, name)]
        if len(bindings) != 1:
            if not bindings and f"{module}.{name}" in self.modules:
                return ("module", f"{module}.{name}")
            return None  # unbound, or bound more than one way: unknown
        stmt = bindings[0]
        if isinstance(stmt, ast.ClassDef):
            return ("class", (module, stmt.name, stmt))
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ("function", module, stmt, None)
        if isinstance(stmt, ast.ImportFrom):
            alias = next(a for a in stmt.names if (a.asname or a.name) == name)
            source = _absolute(module, stmt, is_package=_is_package(self.modules, module))
            if f"{source}.{alias.name}" in self.modules:
                return ("module", f"{source}.{alias.name}")
            if source == module or source not in self.modules:
                return None
            return self._lookup(source, alias.name, depth + 1)
        if isinstance(stmt, ast.Import):
            alias = next(a for a in stmt.names if (a.asname or a.name.split(".")[0]) == name)
            target = alias.name if alias.asname else alias.name.split(".")[0]
            return ("module", target) if target in self.modules or _is_package(self.modules, target) else None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            return ("alias", module, stmt.value)
        if isinstance(stmt, ast.AnnAssign) and stmt.value is not None and _is_type_alias(stmt.annotation):
            return ("alias", module, stmt.value)
        return None

    def _class_member(self, klass: Klass, name: str):
        module, qual, node = klass
        hits = [stmt for stmt in _top_level(node.body) if _binds(stmt, name)]
        if len(hits) != 1:
            return None
        stmt = hits[0]
        if isinstance(stmt, ast.ClassDef):
            return ("class", (module, f"{qual}.{stmt.name}", stmt))
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ("function", module, stmt, klass)
        return None

    def class_of(self, module: str, expr: ast.AST) -> Klass | None:
        found = self.resolve(module, expr)
        return found[1] if found and found[0] == "class" else None

    # --- annotations -------------------------------------------------------------

    def annotation(self, module: str, node: ast.AST | None, depth: int = 0) -> list[Klass] | None:
        """The classes an annotation allows (``None`` dropped), or ``None`` when any
        part is not a plain class this namespace defines."""
        if node is None or depth > _MAX_DEPTH:
            return None
        if isinstance(node, ast.Constant):
            if node.value is None:
                return []
            if isinstance(node.value, str):
                try:
                    node = ast.parse(node.value.strip(), mode="eval").body
                except SyntaxError:
                    return None
                return self.annotation(module, node, depth + 1)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return _join(self.annotation(module, node.left, depth + 1), self.annotation(module, node.right, depth + 1))
        if isinstance(node, ast.Subscript):
            head = _tail(node.value)
            items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if head in _UNION:
                out: list[Klass] | None = []
                for item in items:
                    out = _join(out, self.annotation(module, item, depth + 1))
                return out
            if head in _WRAPS:
                return self.annotation(module, items[0], depth + 1)
            return None
        if isinstance(node, (ast.Name, ast.Attribute)):
            found = self.resolve(module, node)
            if found is None:
                return None
            if found[0] == "class":
                return [found[1]]
            if found[0] == "alias":
                return self.annotation(found[1], found[2], depth + 1)
        return None

    # --- classes -----------------------------------------------------------------

    def bases(self, klass: Klass) -> tuple[list[Klass], list[ast.AST]]:
        """Resolved base classes and the base expressions that did not resolve."""
        module, _qual, node = klass
        known: list[Klass] = []
        unknown: list[ast.AST] = []
        for base in node.bases:
            expr = base.value if isinstance(base, ast.Subscript) else base
            if isinstance(expr, ast.Name) and expr.id == "object":
                continue
            hit = self.class_of(module, expr)
            if hit is None:
                unknown.append(base)
            else:
                known.append(hit)
        return known, unknown

    def is_subclass(self, value: Klass, target: Klass, depth: int = 0) -> bool | None:
        """``value`` is ``target`` or derives from it; ``None`` when that cannot be told."""
        if value[2] is target[2]:
            return True
        if depth > _MAX_DEPTH:
            return None
        known, unknown = self.bases(value)
        answers = [self.is_subclass(base, target, depth + 1) for base in known]
        if any(answer is True for answer in answers):
            return True
        if any(answer is None for answer in answers):
            return None
        # A base from outside both sources cannot derive from one of this repo's
        # own classes; it might from the release's, so that stays unknown.
        if unknown and not self.is_local(target[0]):
            return None
        return False

    def open_ended(self, klass: Klass, depth: int = 0) -> bool:
        """A class other classes may match without deriving from it (structural or
        registered), or one whose bases cannot all be followed for that."""
        _module, _qual, node = klass
        if any(_tail(base.value if isinstance(base, ast.Subscript) else base) == "Protocol" for base in node.bases):
            return True
        if any(
            isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in {"__subclasshook__", "__instancecheck__"}
            for stmt in node.body
        ):
            return True
        if node.keywords or any(_tail(base) in {"ABC", "ABCMeta"} for base in node.bases):
            return True  # a metaclass or ``register`` may answer isinstance its own way
        if depth > _MAX_DEPTH:
            return True
        known, _unknown = self.bases(klass)
        return any(self.open_ended(base, depth + 1) for base in known)

    def member_type(self, klass: Klass, attr: str, depth: int = 0) -> list[Klass] | None:
        """The declared type of ``klass().attr``: an annotated class-level name
        (dataclass / model fields, ``TypedDict`` keys), an annotated ``self.attr``
        in ``__init__``, or a ``@property`` return annotation, searched through
        bases. ``None`` when not declared or not a plain class."""
        if depth > _MAX_DEPTH:
            return None
        module, _qual, node = klass
        for stmt in _top_level(node.body):
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == attr:
                if _tail(stmt.annotation.value if isinstance(stmt.annotation, ast.Subscript) else stmt.annotation) in {"ClassVar", "InitVar"}:
                    return None
                return self.annotation(module, stmt.annotation)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == attr:
                if any(_tail(dec) in _PROPERTY for dec in stmt.decorator_list):
                    return self.annotation(module, stmt.returns)
                return None  # a method, not a value
            if _binds(stmt, attr):
                return None  # assigned without a declared type
        init = _method(node, "__init__")
        if init is not None:
            for stmt in ast.walk(init):
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Attribute)
                    and isinstance(stmt.target.value, ast.Name)
                    and stmt.target.value.id == "self"
                    and stmt.target.attr == attr
                ):
                    return self.annotation(module, stmt.annotation)
        known, unknown = self.bases(klass)
        for base in known:
            found = self.member_type(base, attr, depth + 1)
            if found is not None:
                return found
        return None

    def method(self, klass: Klass, name: str, depth: int = 0) -> tuple[Klass, ast.AST] | None:
        """``name`` defined on ``klass`` or the first resolved base that has it;
        ``None`` when an unresolved base comes first (it may define it)."""
        if depth > _MAX_DEPTH:
            return None
        node = _method(klass[2], name)
        if node is not None:
            return klass, node
        for base in klass[2].bases:
            expr = base.value if isinstance(base, ast.Subscript) else base
            if isinstance(expr, ast.Name) and expr.id == "object":
                continue
            hit = self.class_of(klass[0], expr)
            if hit is None:
                return None
            found = self.method(hit, name, depth + 1)
            if found is not None:
                return found
        return None

    def fields(self, klass: Klass, depth: int = 0) -> list[tuple[str, ast.AST]]:
        """Declared fields: class-level annotations, then ``@property`` returns."""
        out: list[tuple[str, ast.AST]] = []
        if depth > _MAX_DEPTH:
            return out
        for stmt in _top_level(klass[2].body):
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id.startswith("_") or _tail(
                    stmt.annotation.value if isinstance(stmt.annotation, ast.Subscript) else stmt.annotation
                ) == "ClassVar":
                    continue
                out = [item for item in out if item[0] != stmt.target.id] + [(stmt.target.id, stmt.annotation)]
            elif (
                isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                and stmt.returns is not None
                and any(_tail(dec) in _PROPERTY for dec in stmt.decorator_list)
            ):
                out = [item for item in out if item[0] != stmt.name] + [(stmt.name, stmt.returns)]
        known, _unknown = self.bases(klass)
        for base in known:
            for item in self.fields(base, depth + 1):
                if item[0] not in {name for name, _ in out}:
                    out.append(item)
        return out


def qualified(klass: Klass) -> str:
    return f"{klass[0]}.{klass[1]}"


# --- what a value is -----------------------------------------------------------------


def value_classes(ns: Namespace, module: str, func: ast.AST | None, owner: Klass | None, expr: ast.AST, line: int, depth: int = 0) -> list[Klass] | None:
    """The classes ``expr`` certainly has at ``line`` inside ``func`` (a method of
    ``owner`` when given), read from declarations only; ``None`` when unknown."""
    if depth > _MAX_DEPTH:
        return None
    if isinstance(expr, ast.Name):
        return _name_classes(ns, module, func, owner, expr.id, line, depth)
    if isinstance(expr, ast.Attribute):
        if isinstance(expr.value, ast.Name) and owner is not None and _is_self(func, expr.value.id):
            bases: list[Klass] | None = [owner]
        else:
            bases = value_classes(ns, module, func, owner, expr.value, line, depth + 1)
        if not bases:
            return None
        out: list[Klass] | None = []
        for klass in bases:
            out = _join(out, ns.member_type(klass, expr.attr))
        return out or None
    if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str):
        bases = value_classes(ns, module, func, owner, expr.value, line, depth + 1)
        if not bases or not all(_is_keyed(ns, klass) for klass in bases):
            return None
        out = []
        for klass in bases:
            out = _join(out, ns.member_type(klass, expr.slice.value))
        return out or None
    if isinstance(expr, ast.Call):
        target = _callee(ns, module, func, owner, expr.func, line, depth)
        if target is None:
            return None
        if target[0] == "class":
            klass = target[1]
            if ns.method(klass, "__new__") is not None or _is_keyed(ns, klass):
                return None
            return [klass]
        _kind, fmodule, node, _klass = target
        if _overloaded(ns, fmodule, node) or node.returns is None:
            return None
        if isinstance(node, ast.AsyncFunctionDef):
            return None  # a call gives a coroutine until awaited
        out = ns.annotation(fmodule, node.returns)
        return out or None
    if isinstance(expr, ast.Await) and isinstance(expr.value, ast.Call):
        target = _callee(ns, module, func, owner, expr.value.func, line, depth)
        if target is None or target[0] != "function" or not isinstance(target[2], ast.AsyncFunctionDef):
            return None
        if target[2].returns is None or _overloaded(ns, target[1], target[2]):
            return None
        return ns.annotation(target[1], target[2].returns) or None
    return None


def _name_classes(ns, module, func, owner, name: str, line: int, depth: int) -> list[Klass] | None:
    if func is None:
        return None
    if _rebound(func, name):
        binding = _single_binding(func, name, line)
        if binding is None:
            return None
        if isinstance(binding, ast.AnnAssign):
            return ns.annotation(module, binding.annotation) or None
        return value_classes(ns, module, func, owner, binding.value, binding.lineno, depth + 1)
    args = func.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.arg == name:
            return ns.annotation(module, arg.annotation) or None
    return None


def _callee(ns: Namespace, module: str, func, owner: Klass | None, expr: ast.AST, line: int, depth: int):
    """The class or function a call's target names: ``("class", Klass)`` or
    ``("function", module, node, Klass | None)``; ``None`` when unknown."""
    if isinstance(expr, ast.Name):
        if func is not None and (_rebound(func, expr.id) or expr.id in _arg_names(func)):
            return None  # a local name shadows the module's
        found = ns.resolve(module, expr)
    elif isinstance(expr, ast.Attribute):
        if isinstance(expr.value, ast.Name) and owner is not None and _is_self(func, expr.value.id):
            hit = ns.method(owner, expr.attr)
            if hit is None or _decorated_other(hit[1]):
                return None
            return ("function", hit[0][0], hit[1], hit[0])
        if isinstance(expr.value, ast.Name) and func is not None and (
            _rebound(func, expr.value.id) or expr.value.id in _arg_names(func)
        ):
            bases = value_classes(ns, module, func, owner, expr.value, line, depth + 1)
            if not bases or len(bases) != 1:
                return None
            hit = ns.method(bases[0], expr.attr)
            if hit is None or _decorated_other(hit[1]):
                return None
            if _has_decorator(hit[1], "staticmethod", "classmethod"):
                return ("function", hit[0][0], hit[1], None)
            return ("function", hit[0][0], hit[1], hit[0])
        found = ns.resolve(module, expr)
        if found is None:
            base = ns.class_of(module, expr.value)
            if base is not None:
                hit = ns.method(base, expr.attr)
                if hit is not None and _has_decorator(hit[1], "staticmethod", "classmethod"):
                    return ("function", hit[0][0], hit[1], None)
            return None
    else:
        return None
    if found is None:
        return None
    if found[0] == "class":
        return found
    if found[0] == "function":
        node, klass = found[2], found[3]
        if _decorated_other(node):
            return None
        if klass is not None and not _has_decorator(node, "staticmethod", "classmethod"):
            return None  # an unbound method called through its class
        return ("function", found[1], node, None)
    return None


# --- what a call expects -------------------------------------------------------------


def parameters(ns: Namespace, target) -> tuple[str, list[tuple[str, ast.AST | None]], set[str], bool] | None:
    """``(module, positional (name, annotation), keyword-only names, takes **kwargs)``
    of what a call's target accepts; keyword-only annotations are in the list too."""
    if target[0] == "class":
        klass = target[1]
        hit = ns.method(klass, "__init__")
        if hit is not None:
            node = hit[1]
            skip = 1
            fmodule = hit[0][0]
        else:
            known, unknown = ns.bases(klass)
            field_based = _has_decorator_named(klass[2], "dataclass") or bool(unknown)
            if not field_based:
                return None
            names = [
                (name, annotation)
                for name, annotation in ns.fields(klass)
                if _tail(annotation.value if isinstance(annotation, ast.Subscript) else annotation) not in {"ClassVar", "InitVar"}
                and not _is_property(klass, name)
            ]
            return klass[0], names, set(), True
    else:
        _kind, fmodule, node, klass = target
        skip = 1 if klass is not None else 0
        if _has_decorator(node, "classmethod"):
            skip = 1
        elif _has_decorator(node, "staticmethod"):
            skip = 0
    args = node.args
    positional = [(arg.arg, arg.annotation) for arg in (*args.posonlyargs, *args.args)][skip:]
    keyword = [(arg.arg, arg.annotation) for arg in args.kwonlyargs]
    return fmodule, positional + keyword, {name for name, _ in keyword}, args.kwarg is not None


def type_mismatches(
    ns: Namespace, path: str, module: str, tree: ast.Module, span: tuple[int, int],
    release_calls: dict[tuple[int, str], tuple[str, ast.AST]] | None = None, where: str = "this repo",
) -> list[dict]:
    """Arguments in the span whose certain class is not the declared parameter class.

    ``release_calls`` maps ``(line, call target text)`` to the release callable
    ``(module, def)`` a call there was traced to, for targets the names here do
    not reach (a client attribute's method)."""
    out: list[dict] = []
    for func, owner in _functions(module, tree):
        for call in ast.walk(func):
            if not isinstance(call, ast.Call) or not (span[0] <= call.lineno <= span[1]):
                continue
            if _innermost(func, call) is not func:
                continue
            out.extend(_call_mismatches(ns, path, module, func, owner, call, where, release_calls or {}))
    return out


def _call_mismatches(ns, path, module, func, owner, call: ast.Call, where: str, release_calls) -> list[dict]:
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
        return []
    target = _callee(ns, module, func, owner, call.func, call.lineno, 0)
    if target is None:
        traced = release_calls.get((call.lineno, ast.unparse(call.func)))
        target = _release_target(ns, traced) if traced is not None else None
    if target is None:
        return []
    spec = parameters(ns, target)
    if spec is None:
        return []
    fmodule, params, keyword_only, _kwargs = spec
    by_name = dict(params)
    positional = [name for name, _ in params if name not in keyword_only]
    pairs: list[tuple[str, ast.AST]] = []
    if len(call.args) > len(positional):
        return []
    pairs += list(zip(positional, call.args))
    pairs += [(kw.arg, kw.value) for kw in call.keywords if kw.arg in by_name]
    out: list[dict] = []
    for name, value in pairs:
        expected = ns.annotation(fmodule, by_name.get(name))
        if not expected or any(ns.open_ended(klass) for klass in expected):
            continue
        actual = value_classes(ns, module, func, owner, value, call.lineno)
        if not actual:
            continue
        verdicts = [ns.is_subclass(have, want) for have in actual for want in expected]
        if any(verdict is not False for verdict in verdicts):
            continue  # fits, or cannot be told
        callee_name = target[1][1] if target[0] == "class" else target[2].name
        source = where if ns.is_local(fmodule) else where_release(fmodule)
        out.append(
            {
                "path": path,
                "line": call.lineno,
                "call": ast.unparse(call.func),
                "owner": callee_name.split(".")[-1],
                "where": source,
                "param": name,
                "value": ast.unparse(value),
                "actual": actual,
                "expected": expected,
            }
        )
    return out


def _release_target(ns: Namespace, traced: tuple[str, ast.AST]):
    """A traced release callable as a call target; ``None`` for overloaded ones."""
    fmodule, node = traced
    if isinstance(node, ast.ClassDef):
        return ("class", (fmodule, node.name, node))
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or _has_decorator(node, "overload"):
        return None
    tree = ns.modules.get(fmodule)
    if tree is not None and sum(
        1 for item in ast.walk(tree) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == node.name and _has_decorator(item, "overload")
    ):
        return None  # several declared signatures: which one applies is not read here
    args = [*node.args.posonlyargs, *node.args.args]
    bound = bool(args) and args[0].arg in {"self", "cls"} and not _has_decorator(node, "staticmethod")
    return ("function", fmodule, node, ("", "", ast.ClassDef(name="", bases=[], keywords=[], body=[], decorator_list=[])) if bound else None)


def where_release(module: str) -> str:
    return f"the {module.split('.')[0]} release"


def render(ns: Namespace, item: dict) -> str:
    """One finding: both qualified types, the target's fields and how it is made."""
    have = " or ".join(qualified(klass) for klass in item["actual"])
    want = " or ".join(qualified(klass) for klass in item["expected"])
    same = {klass[1].split(".")[-1] for klass in item["actual"]} & {klass[1].split(".")[-1] for klass in item["expected"]}
    plain = (
        f"a different class that only shares the name {sorted(same)[0]}"
        if same
        else "a different class"
    )
    text = (
        f"{item['path']}:{item['line']} {item['call']}(...): {item['param']}= passes {have} "
        f"(from {item['value']}) but {item['owner']} in {item['where']} expects {want}, {plain}; "
        f"build a {want} from the value's fields instead of passing it through"
    )
    for klass in item["expected"]:
        text += f" {_shape(ns, klass)}"
    for klass in item["actual"]:
        text += f" {_shape(ns, klass)}"
    makers = [maker for klass in item["expected"] for maker in _makers(ns, klass)]
    if makers:
        text += f" ({want} is made by: {', '.join(makers[:4])})"
    return text


def _shape(ns: Namespace, klass: Klass) -> str:
    hit = ns.method(klass, "__init__")
    if hit is not None:
        args = hit[1].args
        items = [arg for arg in (*args.posonlyargs, *args.args)][1:] + list(args.kwonlyargs)
        names = [f"{arg.arg}: {_short(arg.annotation)}" if arg.annotation else arg.arg for arg in items]
        return f"({qualified(klass)} takes: {', '.join(names[:12]) or 'nothing'})"
    names = [f"{name}: {_short(annotation)}" for name, annotation in ns.fields(klass)]
    return f"({qualified(klass)} fields: {', '.join(names[:12]) or 'none declared'})"


def _short(annotation: ast.AST) -> str:
    text = ast.unparse(annotation)
    return text if len(text) <= 40 else text[:37] + "..."


def _makers(ns: Namespace, klass: Klass) -> list[str]:
    """Static / class methods on ``klass`` whose declared return is ``klass``."""
    out: list[str] = []
    for stmt in _top_level(klass[2].body):
        if not isinstance(stmt, ast.FunctionDef) or not _has_decorator(stmt, "staticmethod", "classmethod"):
            continue
        returns = ns.annotation(klass[0], stmt.returns) if stmt.returns is not None else None
        if returns and any(item[2] is klass[2] for item in returns):
            args = stmt.args.args[1:] if _has_decorator(stmt, "classmethod") else stmt.args.args
            shown = ", ".join(f"{a.arg}: {ast.unparse(a.annotation)}" if a.annotation else a.arg for a in args)
            out.append(f"{klass[1]}.{stmt.name}({shown})")
    return out


def declared_type_breaks(
    release: dict[str, ast.Module], root: Path, path: str, source: str, span: tuple[int, int], original: str = "",
    release_calls: dict[tuple[int, str], tuple[str, ast.AST]] | None = None,
) -> list[str]:
    """Findings for the rewritten span that the original code did not already have."""
    tree = ast.parse(source)
    module = module_name(path)
    ns = Namespace(repo_modules(root, path, tree), release)
    module = next((name for name in _names_for(path) if ns.modules.get(name) is tree), module)
    found = type_mismatches(ns, path, module, tree, span, release_calls)
    if not found:
        return []
    before: set[tuple] = set()
    if original:
        try:
            old_tree = ast.parse(original)
        except SyntaxError:
            old_tree = None
        if old_tree is not None:
            old_ns = Namespace(repo_modules(root, path, old_tree), release)
            for item in type_mismatches(old_ns, path, module, old_tree, (1, len(original.splitlines()) + 1)):
                before.add(_key(item))
    return [render(ns, item) for item in found if _key(item) not in before]


def _key(item: dict) -> tuple:
    return (
        item["call"],
        item["param"],
        tuple(qualified(klass) for klass in item["actual"]),
        tuple(qualified(klass) for klass in item["expected"]),
    )


# --- small helpers -------------------------------------------------------------------


def _join(left: list | None, right: list | None) -> list | None:
    if left is None or right is None:
        return None
    out = list(left)
    for item in right:
        if not any(item[2] is have[2] for have in out):
            out.append(item)
    return out


def _tail(node: ast.AST | None) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_type_alias(annotation: ast.AST) -> bool:
    return _tail(annotation) == "TypeAlias"


def _top_level(body: list[ast.stmt]):
    for stmt in body:
        if isinstance(stmt, ast.If):
            yield from _top_level(stmt.body)
            yield from _top_level(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            for part in (stmt.body, stmt.orelse, stmt.finalbody, *[h.body for h in stmt.handlers]):
                yield from _top_level(part)
        else:
            yield stmt


def _binds(stmt: ast.stmt, name: str) -> bool:
    if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return stmt.name == name
    if isinstance(stmt, ast.ImportFrom):
        return any((alias.asname or alias.name) == name for alias in stmt.names)
    if isinstance(stmt, ast.Import):
        return any((alias.asname or alias.name.split(".")[0]) == name for alias in stmt.names)
    if isinstance(stmt, ast.Assign):
        return any(_target_binds(target, name) for target in stmt.targets)
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        return _target_binds(stmt.target, name)
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return _target_binds(stmt.target, name)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return any(item.optional_vars is not None and _target_binds(item.optional_vars, name) for item in stmt.items)
    return False


def _target_binds(target: ast.AST, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds(item, name) for item in target.elts)
    if isinstance(target, ast.Starred):
        return _target_binds(target.value, name)
    return False


def _method(node: ast.ClassDef, name: str) -> ast.AST | None:
    hits = [
        stmt
        for stmt in _top_level(node.body)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name
    ]
    return hits[0] if len(hits) == 1 else None


def _has_decorator(node: ast.AST, *names: str) -> bool:
    return any(_tail(dec) in names for dec in getattr(node, "decorator_list", []))


def _has_decorator_named(node: ast.ClassDef, name: str) -> bool:
    return any(_tail(dec) == name for dec in node.decorator_list)


def _decorated_other(node: ast.AST) -> bool:
    """A decorator other than the plain method kinds may change what it takes or returns."""
    return any(_tail(dec) not in {"staticmethod", "classmethod", "abstractmethod"} for dec in node.decorator_list)


def _overloaded(ns: Namespace, module: str, node: ast.AST) -> bool:
    return _has_decorator(node, "overload")


def _is_property(klass: Klass, name: str) -> bool:
    return any(
        isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name and _has_decorator(stmt, *_PROPERTY)
        for stmt in _top_level(klass[2].body)
    )


def _is_keyed(ns: Namespace, klass: Klass, depth: int = 0) -> bool:
    """A ``TypedDict``: its keys are read with ``value["key"]`` and calling it gives a dict."""
    if depth > _MAX_DEPTH:
        return False
    if any(_tail(base) == "TypedDict" for base in klass[2].bases):
        return True
    known, _unknown = ns.bases(klass)
    return any(_is_keyed(ns, base, depth + 1) for base in known)


def _is_self(func: ast.AST | None, name: str) -> bool:
    if func is None or _has_decorator(func, "staticmethod", "classmethod"):
        return False
    args = [*func.args.posonlyargs, *func.args.args]
    return bool(args) and args[0].arg == name and not _rebound(func, name)


def _arg_names(func: ast.AST) -> set[str]:
    args = func.args
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names |= {arg.arg for arg in (args.vararg, args.kwarg) if arg is not None}
    return names


def _own_nodes(func: ast.AST):
    """Nodes of ``func``'s own body, not of functions or classes nested in it."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _rebound(func: ast.AST, name: str) -> bool:
    for node in _own_nodes(func):
        if isinstance(node, ast.stmt) and _binds(node, name):
            return True
        if isinstance(node, ast.NamedExpr) and _target_binds(node.target, name):
            return True
        if isinstance(node, ast.comprehension) and _target_binds(node.target, name):
            return True
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return True
        if isinstance(node, (ast.Delete, ast.Match)):
            return True  # rare forms: do not reason about them
    return False


def _single_binding(func: ast.AST, name: str, line: int):
    """The only statement in ``func`` binding ``name``, when it is a plain
    assignment before ``line`` outside any loop or branch and the name is not
    a parameter."""
    if name in _arg_names(func):
        return None
    hits = []
    for node in _own_nodes(func):
        if isinstance(node, ast.stmt) and _binds(node, name):
            hits.append(node)
        elif isinstance(node, (ast.NamedExpr, ast.comprehension, ast.ExceptHandler, ast.Global, ast.Nonlocal, ast.Delete, ast.Match)):
            if _rebound_by(node, name):
                return None
    if len(hits) != 1:
        return None
    stmt = hits[0]
    if stmt not in func.body or stmt.lineno >= line:
        return None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt
    return None


def _rebound_by(node: ast.AST, name: str) -> bool:
    if isinstance(node, (ast.NamedExpr, ast.comprehension)):
        return _target_binds(node.target, name)
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return name in node.names
    return True


def _functions(module: str, tree: ast.Module):
    """``(function, enclosing class)`` for each function, methods included."""
    def walk(body, owner):
        for stmt in _top_level(body):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield stmt, owner
                yield from walk(stmt.body, None)
            elif isinstance(stmt, ast.ClassDef):
                qual = stmt.name if owner is None else f"{owner[1]}.{stmt.name}"
                yield from walk(stmt.body, (module, qual, stmt))
    yield from walk(tree.body, None)


def _innermost(func: ast.AST, call: ast.Call) -> ast.AST:
    for node in ast.walk(func):
        if node is func or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if any(child is call for child in ast.walk(node)):
            return node
    return func
