"""Dict keys a repo callable reads, against the keys a rewritten call passes it.

A fix that hands a dict literal (or a name bound to one) to a function or
class/static method of this repo is checked against what that callable reads
from the parameter: ``param["k"]`` needs the key, ``param.get("k")`` and
``"k" in param`` do not. Reads in a repo helper the parameter is handed to
count too, one level down. A finding is a required key the dict lacks, or a
dict none of whose keys the callable reads. Nothing here knows any library.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from conduit.apply.declared import (
    Namespace,
    _SKIP_DIRS,
    _callee,
    _functions,
    _has_decorator,
    _innermost,
    _names_for,
    _own_nodes,
    _rebound,
    _single_binding,
    module_name,
    parameters,
    repo_modules,
)

_CATCHES = {"KeyError", "LookupError", "Exception", "BaseException"}
_OPTIONAL_CALLS = {"get"}
_MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"}
_HELPER_DEPTH = 1


@dataclass
class Reads:
    """Keys a callable reads from one parameter."""

    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    #: The parameter is also used in ways not read here (iterated, unpacked, ...).
    opaque: bool = False

    def merge(self, other: "Reads") -> None:
        self.required |= other.required
        self.optional |= other.optional
        self.opaque = self.opaque or other.opaque

    @property
    def keys(self) -> set[str]:
        return self.required | self.optional


def key_breaks(ns: Namespace, path: str, module: str, tree: ast.Module, span: tuple[int, int], where: str = "this repo") -> list[dict]:
    """Calls in the span passing a dict whose keys do not fit what the repo callable reads."""
    out: list[dict] = []
    for func, owner in _functions(module, tree):
        for call in ast.walk(func):
            if not isinstance(call, ast.Call) or not (span[0] <= call.lineno <= span[1]):
                continue
            if _innermost(func, call) is not func:
                continue
            out.extend(_call_breaks(ns, path, module, func, owner, call, where))
    return out


def _call_breaks(ns, path, module, func, owner, call: ast.Call, where: str) -> list[dict]:
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
        return []
    target = _callee(ns, module, func, owner, call.func, call.lineno, 0)
    found = _body(ns, target)
    if found is None:
        return []
    fmodule, node, klass, name = found
    pairs = _pairs(ns, target, call)
    if pairs is None:
        return []
    out: list[dict] = []
    for param, value in pairs:
        supplied = _supplied(func, value, call)
        if supplied is None:
            continue
        reads = param_reads(ns, fmodule, node, klass, param, 0)
        if reads is None or not reads.keys:
            continue
        missing = sorted(reads.required - supplied)
        disjoint = bool(supplied) and not reads.opaque and not (supplied & reads.keys)
        if not missing and not disjoint:
            continue
        out.append(
            {
                "path": path,
                "line": call.lineno,
                "call": ast.unparse(call.func),
                "owner": name,
                "where": where,
                "param": param,
                "value": ast.unparse(value),
                "supplied": sorted(supplied),
                "required": sorted(reads.required),
                "optional": sorted(reads.optional - reads.required),
                "missing": missing,
                "opaque": reads.opaque,
                "node": node,
            }
        )
    return out


def example_calls(ns: Namespace, root: Path, item: dict, *, limit: int = 2) -> list[str]:
    """Up to ``limit`` other calls that already pass a valid argument to this callee.

    The callee is the function object the failing call resolved to, not its name:
    another function that happens to share the name does not count. A call counts
    when that same object is what it invokes and the argument is a dict carrying
    the keys it reads, or any call (a value the repo already passes, such as
    ``row.to_dict()``)."""
    node = item.get("node")
    param = item.get("param") or ""
    if node is None or not param or limit <= 0:
        return []
    reads = Reads(
        required=set(item.get("required") or []),
        optional=set(item.get("optional") or []),
        opaque=bool(item.get("opaque")),
    )
    skip = (item.get("path") or "", int(item.get("line") or 0))
    hits: list[tuple[str, int, str]] = []
    seen: set[int] = set()
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.split("/")[:-1]):
            continue
        module = module_name(rel)
        tree = ns.modules.get(module)
        if tree is None or id(tree) in seen or not ns.is_local(module):
            continue
        seen.add(id(tree))
        for func, owner in _functions(module, tree):
            for call in ast.walk(func):
                if not isinstance(call, ast.Call) or _innermost(func, call) is not func:
                    continue
                if (rel, call.lineno) == skip:
                    continue
                value = _argument_for(ns, module, func, owner, call, node, param)
                if value is None or not _passes_valid(func, value, call, reads):
                    continue
                hits.append((rel, call.lineno, ast.unparse(call)))
    hits.sort()
    shown: list[str] = []
    for rel, line, expr in hits:
        text = f"{rel}:{line} {expr}"
        if text not in shown:
            shown.append(text)
        if len(shown) == limit:
            break
    return shown


def _argument_for(ns, module, func, owner, call: ast.Call, node: ast.AST, param: str) -> ast.AST | None:
    """The argument ``call`` passes as ``param`` when ``call`` invokes ``node``."""
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
        return None
    target = _callee(ns, module, func, owner, call.func, call.lineno, 0)
    found = _body(ns, target)
    if found is None or found[1] is not node:
        return None
    pairs = _pairs(ns, target, call)
    if pairs is None:
        return None
    for name, value in pairs:
        if name == param:
            return value
    return None


def _passes_valid(func: ast.AST, value: ast.AST, call: ast.Call, reads: Reads) -> bool:
    """A dict whose keys fit ``reads``, or a call the repo already passes through."""
    if isinstance(value, ast.Call):
        return True
    supplied = _supplied(func, value, call)
    if supplied is None or reads.required - supplied:
        return False
    if supplied and not reads.opaque and not (supplied & reads.keys):
        return False
    return True


def _body(ns: Namespace, target):
    """``(module, function node, enclosing class, shown name)`` of a repo callable."""
    if target is None:
        return None
    if target[0] == "class":
        klass = target[1]
        hit = ns.method(klass, "__init__")
        if hit is None or not ns.is_local(hit[0][0]):
            return None
        return hit[0][0], hit[1], hit[0], klass[1].split(".")[-1]
    _kind, fmodule, node, _klass = target
    if not ns.is_local(fmodule):
        return None
    klass = _owner(ns, fmodule, node)
    shown = f"{klass[1].split('.')[-1]}.{node.name}" if klass is not None else node.name
    return fmodule, node, klass, shown


def _owner(ns: Namespace, module: str, node: ast.AST):
    tree = ns.modules.get(module)
    if tree is None:
        return None
    for func, owner in _functions(module, tree):
        if func is node:
            return owner
    return None


def _pairs(ns: Namespace, target, call: ast.Call) -> list[tuple[str, ast.AST]] | None:
    spec = parameters(ns, target)
    if spec is None:
        return None
    _fmodule, params, keyword_only, _kwargs = spec
    names = {name for name, _ in params}
    positional = [name for name, _ in params if name not in keyword_only]
    if len(call.args) > len(positional):
        return None
    return list(zip(positional, call.args)) + [(kw.arg, kw.value) for kw in call.keywords if kw.arg in names]


def _supplied(func: ast.AST, value: ast.AST, call: ast.Call) -> set[str] | None:
    """The constant keys of a dict literal, or of the one dict literal a name is bound to."""
    if isinstance(value, ast.Name):
        stmt = _single_binding(func, value.id, call.lineno)
        if stmt is None or stmt.value is None or _changed_after(func, value.id, stmt, call):
            return None
        value = stmt.value
    if not isinstance(value, ast.Dict):
        return None
    keys: set[str] = set()
    for key in value.keys:
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None  # ``**other`` or a computed key: the full set is not known
        keys.add(key.value)
    return keys


def _changed_after(func: ast.AST, name: str, stmt: ast.stmt, call: ast.Call) -> bool:
    """Whether ``name``'s dict may gain or lose keys between its binding and the call."""
    for node in _own_nodes(func):
        if isinstance(node, ast.Subscript) and _is(node.value, name) and not isinstance(node.ctx, ast.Load):
            return True
        if isinstance(node, ast.Attribute) and _is(node.value, name) and node.attr in _MUTATORS:
            return True
        if isinstance(node, ast.Call) and node is not call and any(
            _is(arg, name) for arg in (*node.args, *(kw.value for kw in node.keywords))
        ):
            return True  # handed to something else first, which may change it
    return False


def param_reads(ns: Namespace, module: str, node: ast.AST, klass, param: str, depth: int) -> Reads | None:
    """What ``node`` reads from its parameter ``param``; ``None`` when it rebinds it."""
    if _rebound(node, param):
        return None
    reads = Reads()
    guarded = _guarded(node)
    stored: set[str] = set()
    claimed: set[int] = set()
    for item in _own_nodes(node):
        if isinstance(item, ast.Subscript) and _is(item.value, param):
            claimed.add(id(item.value))
            key = _constant(item.slice)
            if key is None:
                reads.opaque = True
            elif isinstance(item.ctx, ast.Load):
                (reads.optional if id(item) in guarded else reads.required).add(key)
            else:
                stored.add(key)
        elif isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and _is(item.func.value, param):
            claimed.add(id(item.func.value))
            key = _constant(item.args[0]) if item.args else None
            if item.func.attr in _OPTIONAL_CALLS and key is not None:
                reads.optional.add(key)
            elif item.func.attr == "pop" and key is not None:
                (reads.optional if len(item.args) > 1 else reads.required).add(key)
            else:
                reads.opaque = True
        elif isinstance(item, ast.Compare) and len(item.ops) == 1 and isinstance(item.ops[0], (ast.In, ast.NotIn)):
            if _is(item.comparators[0], param):
                claimed.add(id(item.comparators[0]))
                key = _constant(item.left)
                if key is not None:
                    reads.optional.add(key)
                else:
                    reads.opaque = True
        elif isinstance(item, ast.Call) and depth < _HELPER_DEPTH:
            for target_param, arg in _helper_args(ns, item, param):
                helper = _callee(ns, module, node, klass, item.func, item.lineno, 0)
                if helper is None and klass is not None:
                    helper = _own_method(ns, node, klass, item.func)
                found = _body(ns, helper)
                pairs = _pairs(ns, helper, item) if found is not None else None
                if found is None or pairs is None:
                    continue
                name = next((p for p, value in pairs if value is arg), None)
                if name is None:
                    continue
                sub = param_reads(ns, found[0], found[1], found[2], name, depth + 1)
                if sub is None:
                    reads.opaque = True
                else:
                    reads.merge(sub)
                claimed.add(id(arg))
    # Any other use of the parameter (iteration, ``**param``, a call this does not
    # follow) may read keys this cannot see.
    for item in _own_nodes(node):
        if isinstance(item, ast.Name) and item.id == param and id(item) not in claimed:
            reads.opaque = True
            break
    optional_keys = reads.optional
    reads.required -= stored | optional_keys
    return reads


def _helper_args(ns: Namespace, call: ast.Call, param: str):
    for arg in (*call.args, *(kw.value for kw in call.keywords)):
        if _is(arg, param):
            yield None, arg


def _own_method(ns: Namespace, func: ast.AST, klass, expr: ast.AST):
    """``cls.helper`` / ``self.helper`` inside a class or static method's own class."""
    if not (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)):
        return None
    args = [*func.args.posonlyargs, *func.args.args]
    if not args or args[0].arg != expr.value.id or _has_decorator(func, "staticmethod"):
        return None
    hit = ns.method(klass, expr.attr)
    if hit is None:
        return None
    bound = None if _has_decorator(hit[1], "staticmethod", "classmethod") else hit[0]
    return ("function", hit[0][0], hit[1], bound)


def _guarded(node: ast.AST) -> set[int]:
    """Subscripts inside a ``try`` that catches a missing key."""
    out: set[int] = set()
    for item in _own_nodes(node):
        if not isinstance(item, ast.Try):
            continue
        if not any(handler.type is None or _catches(handler.type) for handler in item.handlers):
            continue
        for stmt in item.body:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Subscript):
                    out.add(id(inner))
    return out


def _catches(expr: ast.AST) -> bool:
    names = expr.elts if isinstance(expr, ast.Tuple) else [expr]
    return any(
        (item.id if isinstance(item, ast.Name) else item.attr if isinstance(item, ast.Attribute) else "") in _CATCHES
        for item in names
    )


def _is(expr: ast.AST, name: str) -> bool:
    return isinstance(expr, ast.Name) and expr.id == name


def _constant(expr: ast.AST) -> str | None:
    return expr.value if isinstance(expr, ast.Constant) and isinstance(expr.value, str) else None


def key_hints(root: Path, path: str, source: str, span: tuple[int, int]) -> list[str]:
    """Keys a repo callable in ``span`` reads, for the first rewrite prompt.

    ``span`` is the enclosing statement or the function the model is asked to
    rewrite. A line is included when a call there resolves to a function or
    classmethod in this repo and ``param_reads`` sees that parameter read with
    ``p['k']``, ``.get`` or ``.pop`` (a helper one level down counts). Nothing
    is returned when no call qualifies, so a prompt for any other site stays
    as it was."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module = module_name(path)
    ns = Namespace(repo_modules(root, path, tree), {})
    module = next((name for name in _names_for(path) if ns.modules.get(name) is tree), module)
    hints: list[str] = []
    seen: set[tuple[int, str]] = set()

    def consider(func, owner, call: ast.Call) -> None:
        if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
            return
        target = _callee(ns, module, func, owner, call.func, call.lineno, 0)
        found = _body(ns, target)
        if found is None:
            return
        fmodule, node, klass, name = found
        spec = parameters(ns, target)
        if spec is None:
            return
        for param, _annotation in spec[1]:
            if (id(node), param) in seen:
                continue
            reads = param_reads(ns, fmodule, node, klass, param, 0)
            if reads is None or not reads.keys or not _lookup_reads(ns, fmodule, node, klass, param, 0):
                continue
            seen.add((id(node), param))
            required = sorted(reads.required)
            optional = sorted(reads.optional - reads.required)
            item = {
                "path": path,
                "line": call.lineno,
                "param": param,
                "required": required,
                "optional": optional,
                "opaque": reads.opaque,
                "node": node,
            }
            shown = ", ".join(repr(key) for key in required) or "none"
            text = f"{name} parameter {param} reads keys: required {shown}"
            if optional:
                text += "; optional " + ", ".join(repr(key) for key in optional)
            examples = example_calls(ns, root, item)
            if examples:
                text += "\nExisting calls that pass a valid argument: " + "; ".join(examples)
            hints.append(text)

    for func, owner in _functions(module, tree):
        for call in ast.walk(func):
            if not isinstance(call, ast.Call) or not (span[0] <= call.lineno <= span[1]):
                continue
            if _innermost(func, call) is not func:
                continue
            consider(func, owner, call)
    for stmt in tree.body:
        end = stmt.end_lineno or stmt.lineno
        if end < span[0] or stmt.lineno > span[1]:
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for call in ast.walk(stmt):
            if isinstance(call, ast.Call) and span[0] <= call.lineno <= span[1]:
                consider(None, None, call)
    return hints


def _lookup_reads(ns, module: str, node: ast.AST, klass, param: str, depth: int) -> bool:
    """Whether ``param`` is read with a subscript, ``.get`` or ``.pop``.

    A repo helper the parameter is handed to counts, one level down, the same
    way ``param_reads`` does. A membership test alone does not."""
    for item in _own_nodes(node):
        if isinstance(item, ast.Subscript) and _is(item.value, param) and isinstance(item.ctx, ast.Load):
            if _constant(item.slice) is not None:
                return True
        elif (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and _is(item.func.value, param)
            and item.func.attr in (*_OPTIONAL_CALLS, "pop")
            and item.args
            and _constant(item.args[0]) is not None
        ):
            return True
        elif isinstance(item, ast.Call) and depth < _HELPER_DEPTH:
            helper = _callee(ns, module, node, klass, item.func, item.lineno, 0)
            if helper is None and klass is not None:
                helper = _own_method(ns, node, klass, item.func)
            found = _body(ns, helper)
            pairs = _pairs(ns, helper, item) if found is not None else None
            if found is None or pairs is None:
                continue
            for _target_param, arg in _helper_args(ns, item, param):
                name = next((pname for pname, value in pairs if value is arg), None)
                if name and _lookup_reads(ns, found[0], found[1], found[2], name, depth + 1):
                    return True
    return False


def render(item: dict, examples: list[str] | None = None) -> str:
    """One finding, in the argument-finding shape the retry loop and reasons read.

    ``examples`` are other calls that already pass a valid argument, so a retry
    sees the keys this callee reads and calls that fit them."""
    supplied = ", ".join(repr(key) for key in item["supplied"]) or "none"
    reads = ", ".join(repr(key) for key in item["required"]) or "none"
    optional = ", ".join(repr(key) for key in item["optional"])
    text = (
        f"{item['path']}:{item['line']} {item['call']}(...): {item['param']}= passes a dict with keys {supplied} "
        f"(from {item['value']}) but {item['owner']} in {item['where']} reads keys: required {reads}"
    )
    if optional:
        text += f"; optional {optional}"
    if item["missing"]:
        text += f"; missing required {', '.join(repr(key) for key in item['missing'])}"
    else:
        text += "; none of the passed keys is read"
    text += f"; pass a dict with the keys {item['owner']} reads"
    if examples:
        text += "\nExisting calls that pass a valid argument: " + "; ".join(examples)
    return text


def key(item: dict) -> tuple:
    return (item["call"], item["param"], tuple(item["supplied"]), tuple(item["missing"]))
