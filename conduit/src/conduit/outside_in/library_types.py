"""Return classes and attribute types read from a distribution's own files.

A method call's result is the class named by its annotation, by a sibling
stub, or by a ``return`` / ``yield`` expression. A method the class does not
define is taken from a base. The next attribute is then that class's method,
so a chained call binds to the class the library actually returns.
"""

from __future__ import annotations

import ast
import json
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen

from conduit.analysis_cache import cache_root, file_sha256, read_json, write_json
from conduit.deps.manifests import read_installed

_MAX_FILES = 800
_MAX_DOWNLOAD = 8_000_000
_DEPTH = 8
_FIXTURE_WHEELS = (
    Path(__file__).resolve().parents[3] / "fixtures" / "impact" / "wheels",
    Path(__file__).resolve().parents[3] / "fixtures" / "impact" / "wheels" / "deps",
)


def _libtypes_cache() -> Path:
    return cache_root() / "libtypes"


def _wheel_dirs() -> tuple[Path, ...]:
    return (cache_root() / "wheels", Path("/tmp/conduit-wheels"), *_FIXTURE_WHEELS)


@dataclass
class _Mod:
    name: str
    tree: ast.AST
    bindings: dict[str, str]
    classes: dict[str, ast.ClassDef]
    functions: dict[str, ast.AST]
    self_assigns: dict[tuple[str, str], list[ast.expr]] = field(default_factory=dict)
    class_ann: dict[tuple[str, str], ast.expr] = field(default_factory=dict)


@dataclass
class _DistFacts:
    """JSON-friendly return / attribute / delegation facts for one wheel."""

    returns: dict[str, list[str]]
    attribute_types: dict[str, str]
    delegated: dict[str, list[str]]
    init_params: dict[str, list[str]]
    members: dict[str, str]

    def to_json(self) -> dict:
        return {
            "returns": self.returns,
            "attribute_types": self.attribute_types,
            "delegated": self.delegated,
            "init_params": self.init_params,
            "members": self.members,
        }

    @classmethod
    def from_json(cls, payload: object) -> _DistFacts | None:
        if not isinstance(payload, dict):
            return None
        returns = payload.get("returns")
        attribute_types = payload.get("attribute_types")
        delegated = payload.get("delegated")
        init_params = payload.get("init_params")
        members = payload.get("members")
        if not all(
            isinstance(item, dict)
            for item in (returns, attribute_types, delegated, init_params, members)
        ):
            return None
        return cls(
            returns={str(k): [str(v) for v in vals] for k, vals in returns.items() if isinstance(vals, list)},
            attribute_types={str(k): str(v) for k, v in attribute_types.items()},
            delegated={
                str(k): [str(v) for v in vals] for k, vals in delegated.items() if isinstance(vals, list)
            },
            init_params={
                str(k): [str(v) for v in vals] for k, vals in init_params.items() if isinstance(vals, list)
            },
            members={str(k): str(v) for k, v in members.items()},
        )


class LibraryTypes:
    """Types for one consumer repo, loaded from installed or pinned distributions."""

    def __init__(self, root: Path | None) -> None:
        self.root = root
        self._pins = read_installed(root) if root is not None else {}
        self._dist: dict[str, dict[str, _Mod] | None] = {}
        self._facts: dict[str, _DistFacts] = {}
        self._returns: dict[str, tuple[str, ...]] = {}
        self._attribute_types: dict[tuple[str, str], str] = {}
        self._stack: set[str] = set()
        self._defining: set[str] = set()
        self._ctor_args: dict[tuple[str, str], str] = {}
        self._extracting = False

    def delegated_calls(self, qualname: str) -> tuple[str, ...]:
        """External calls this function makes, following ``self`` methods.

        The names come from the function body. A call into another
        distribution is kept. A call to a method of the same class is
        followed. Empty when the body is not in the installed files.
        """
        qualname = _clean(qualname)
        facts = self._facts_for(qualname)
        if facts is not None and not self._extracting:
            return tuple(facts.delegated.get(qualname, ()))
        found = self._delegated_calls(qualname, set())
        return tuple(dict.fromkeys(found))

    def init_params(self, qualname: str) -> tuple[str, ...]:
        """Positional ``__init__`` parameter names, excluding ``self``."""
        qualname = _clean(qualname)
        facts = self._facts_for(qualname)
        if facts is not None and not self._extracting:
            return tuple(facts.init_params.get(qualname, ()))
        defined = self._define(qualname)
        if not defined or defined[2] != "class":
            return ()
        init = defined[0].functions.get(f"{qualname}.__init__")
        if not isinstance(init, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ()
        return tuple(
            arg.arg
            for arg in (*init.args.posonlyargs, *init.args.args)
            if arg.arg not in {"self", "cls"}
        )

    def remember_init_arg(self, qualname: str, name: str, typed: str) -> None:
        """Remember a class passed for one constructor parameter at a call site."""
        qualname = _clean(qualname)
        name = (name or "").strip()
        typed = _clean(typed)
        if not qualname or not name or not typed:
            return
        key = (qualname, name)
        if key in self._ctor_args:
            return
        self._ctor_args[key] = typed
        self._attribute_types.pop(key, None)

    def call_return(self, qualname: str) -> tuple[str, ...]:
        """Classes a call of ``qualname`` produces. Empty when unknown."""
        qualname = _clean(qualname)
        if not qualname or qualname in self._stack:
            return ()
        facts = self._facts_for(qualname)
        if facts is not None and not self._extracting:
            return tuple(facts.returns.get(qualname, ()))
        cached = self._returns.get(qualname)
        if cached is not None:
            return cached
        self._stack.add(qualname)
        try:
            found = self._call_return(qualname)
        finally:
            self._stack.discard(qualname)
        self._returns[qualname] = found
        return found

    def instance_attribute(self, qualname: str, name: str) -> str:
        """Class stored on ``qualname.name``, when that attribute is data."""
        qualname = _clean(qualname)
        name = (name or "").strip()
        if not qualname or not name:
            return ""
        facts = self._facts_for(qualname)
        if facts is not None and not self._extracting:
            return facts.attribute_types.get(f"{qualname}.{name}", "")
        key = (qualname, name)
        if key in self._attribute_types:
            return self._attribute_types[key]
        token = f"{qualname}.{name}"
        if token in self._stack:
            return ""
        self._stack.add(token)
        try:
            found = self._attribute(qualname, name)
        finally:
            self._stack.discard(token)
        self._attribute_types[key] = found
        return found

    def has_member(self, qualname: str, name: str) -> bool:
        """True when this class defines ``name`` as a method or a data attribute.

        A method the class does not define is taken from a base. A missing
        name that ``__getattr__`` would answer counts too, because that is
        the library's own code.
        """
        qualname = _clean(qualname)
        name = (name or "").strip()
        if not qualname or not name or not name.isidentifier():
            return False
        facts = self._facts_for(qualname)
        if facts is not None and not self._extracting:
            key = f"{qualname}.{name}"
            return key in facts.members or key in facts.attribute_types
        if self._define(f"{qualname}.{name}"):
            return True
        return bool(self.instance_attribute(qualname, name))

    def member_owner(self, qualname: str, name: str) -> str:
        """Class that defines ``name``, walking bases when the subclass does not.

        Empty when the name is only answered by ``__getattr__`` or is unknown.
        """
        qualname = _clean(qualname)
        name = (name or "").strip()
        if not qualname or not name or not name.isidentifier():
            return ""
        facts = self._facts_for(qualname)
        if facts is not None and not self._extracting:
            return facts.members.get(f"{qualname}.{name}", "")
        defined = self._define(f"{qualname}.{name}")
        if not defined:
            return ""
        mod, node, kind = defined
        if kind == "class":
            return self._class_qual(mod, node) if node is not None else ""
        owner, _method = _owner_method(self._qual_of(mod, node) or "")
        return owner

    def _facts_for(self, qualname: str) -> _DistFacts | None:
        top = qualname.split(".", 1)[0] if qualname else ""
        if not top:
            return None
        self._modules(top)
        return self._facts.get(top)

    def _delegated_calls(self, qualname: str, seen: set[str]) -> list[str]:
        if not qualname or qualname in seen or len(seen) > 8:
            return []
        seen.add(qualname)
        defined = self._define(qualname)
        if not defined or not isinstance(defined[1], (ast.FunctionDef, ast.AsyncFunctionDef)):
            return []
        mod, node, _kind = defined
        owner, _method = _owner_method(self._qual_of(mod, node) or qualname)
        found: list[str] = []
        for call in _call_nodes(node):
            target = _called_qual(mod, call, owner)
            if not target:
                continue
            if owner and (target == owner or target.startswith(owner + ".")):
                found.extend(self._delegated_calls(target, seen))
                continue
            same_package = target.split(".", 1)[0] == qualname.split(".", 1)[0]
            if same_package and self._define(target):
                found.extend(self._delegated_calls(target, seen))
                continue
            found.append(target)
        return found

    def _call_return(self, qualname: str) -> tuple[str, ...]:
        defined = self._define(qualname)
        if not defined:
            return ()
        mod, node, kind = defined
        if kind == "class":
            qual = self._class_qual(mod, node) if node is not None else ""
            return (qual,) if qual else ()
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ()
        owner, method = _owner_method(self._qual_of(mod, node))
        annotated = self._annotation_classes(mod, node.returns, owner)
        if annotated:
            return tuple(sorted(annotated))
        stub = self._stub_return(mod, node, owner)
        if stub:
            return tuple(sorted(stub))
        found: set[str] = set()
        for expr in _return_exprs(node):
            found |= self._expr_classes(mod, expr, node, owner, 0)
        if not found and owner:
            found |= self._decorator_owner(mod, node, owner)
        return tuple(sorted(found))

    def _decorator_owner(self, mod: _Mod, node: ast.AST, owner: str) -> set[str]:
        """A wrapper that ``return self`` gives the method back its own class.

        Some libraries copy the instance in a decorator and return that copy,
        while the method body itself has no ``return``. The wrapper's ``self``
        is the instance the method was called on.
        """
        for deco in getattr(node, "decorator_list", []) or []:
            func = self._decorator_function(mod, deco)
            if func is not None and _wrapper_returns_self(func):
                return {owner}
        return set()

    def _decorator_function(self, mod: _Mod, deco: ast.AST) -> ast.AST | None:
        expr = deco.func if isinstance(deco, ast.Call) else deco
        qual = ""
        if isinstance(expr, ast.Name):
            qual = mod.bindings.get(expr.id, "")
        elif isinstance(expr, ast.Attribute):
            qual = _dotted(mod, expr)
        if not qual:
            return None
        defined = self._define(qual)
        if defined and isinstance(defined[1], (ast.FunctionDef, ast.AsyncFunctionDef)):
            return defined[1]
        return None

    def _attribute(self, qualname: str, name: str) -> str:
        defined = self._define(qualname)
        if not defined or defined[2] != "class" or defined[1] is None:
            return ""
        mod, node, _kind = defined
        class_qual = self._class_qual(mod, node)
        if f"{class_qual}.{name}" in mod.functions or any(
            item.endswith("." + name) and item.rsplit(".", 1)[0] == class_qual
            for item in mod.functions
        ):
            return ""
        ann = mod.class_ann.get((class_qual, name))
        if ann is not None:
            classes = self._annotation_classes(mod, ann, class_qual)
            if classes:
                return sorted(classes)[0]
        for expr in mod.self_assigns.get((class_qual, name), []):
            classes = self._expr_classes(mod, expr, None, class_qual, 0)
            if classes:
                return sorted(classes)[0]
        for base in self._bases(mod, node):
            typed = self.instance_attribute(base, name)
            if typed:
                return typed
        # A missing attribute is whatever ``__getattr__`` returns. That method
        # is the library's own code, including a call to ``__getitem__``.
        dynamic = self.call_return(f"{class_qual}.__getattr__")
        return dynamic[0] if dynamic else ""

    def _define(self, qualname: str) -> tuple[_Mod, ast.AST | None, str] | None:
        qualname = self._resolve_alias(qualname)
        if not qualname or qualname in self._defining:
            return None
        self._defining.add(qualname)
        try:
            return self._define_uncached(qualname)
        finally:
            self._defining.discard(qualname)

    def _define_uncached(self, qualname: str) -> tuple[_Mod, ast.AST | None, str] | None:
        top = qualname.split(".", 1)[0]
        mods = self._modules(top)
        if not mods:
            return None
        for mod in mods.values():
            node = mod.classes.get(qualname)
            if node is not None:
                return mod, node, "class"
            fn = mod.functions.get(qualname)
            if fn is not None:
                return mod, fn, "function"
        if qualname in mods:
            return None
        # ``pkg.mod.Class.method`` inherited from a base.
        owner, method = _owner_method(qualname)
        if not owner or not method:
            return None
        defined = self._define(owner)
        if not defined or defined[2] != "class" or defined[1] is None:
            return None
        for base in self._bases(defined[0], defined[1]):
            found = self._define(f"{base}.{method}")
            if found:
                return found
        return None

    def _bases(self, mod: _Mod, node: ast.ClassDef) -> list[str]:
        found: list[str] = []
        for base in node.bases:
            for qual in self._expr_classes(mod, base, None, "", 0):
                if qual not in found:
                    found.append(qual)
        return found

    def _resolve_alias(self, qualname: str) -> str:
        parts = [part for part in qualname.split(".") if part]
        if len(parts) < 2:
            return qualname
        top = parts[0]
        mods = self._modules(top)
        if not mods:
            return qualname
        # Walk module imports: ``pkg.orm.Name`` may be ``pkg.orm.session.Name``.
        module = top
        index = 1
        while index < len(parts):
            mod = mods.get(module)
            name = parts[index]
            if mod is None:
                break
            qual = f"{module}.{name}"
            if qual in mod.classes or qual in mod.functions:
                rest = ".".join(parts[index:])
                return f"{module}.{rest}"
            target = mod.bindings.get(name, "")
            if not target:
                child = f"{module}.{name}"
                if child in mods:
                    module = child
                    index += 1
                    continue
                break
            if index + 1 < len(parts):
                if self._is_defined(mods, target):
                    rest = ".".join(parts[index + 1 :])
                    return f"{target}.{rest}"
                module = target
                index += 1
                continue
            return target
        return qualname

    def _is_defined(self, mods: dict[str, _Mod], qualname: str) -> bool:
        for mod in mods.values():
            if qualname in mod.classes or qualname in mod.functions:
                return True
        return False

    def _class_qual(self, mod: _Mod, node: ast.AST) -> str:
        if isinstance(node, ast.ClassDef):
            for qual, item in mod.classes.items():
                if item is node:
                    return qual
        return ""

    def _qual_of(self, mod: _Mod, node: ast.AST) -> str:
        for qual, item in mod.functions.items():
            if item is node:
                return qual
        return self._class_qual(mod, node)

    def _stub_return(self, mod: _Mod, node: ast.AST, owner: str) -> set[str]:
        name = getattr(node, "name", "")
        if not name:
            return set()
        stub = self._modules_of(mod.name).get(mod.name + ".pyi-overlay")
        if stub is None:
            return set()
        qual = self._qual_of(mod, node)
        other = stub.functions.get(qual)
        if not isinstance(other, (ast.FunctionDef, ast.AsyncFunctionDef)) or other.returns is None:
            return set()
        return self._annotation_classes(stub, other.returns, owner)

    def _annotation_classes(self, mod: _Mod, node: ast.AST | None, owner: str) -> set[str]:
        if node is None:
            return set()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip().strip("'\"")
            if text and any(mark in text for mark in "[]|,") :
                try:
                    parsed = ast.parse(text, mode="eval").body
                except SyntaxError:
                    parsed = None
                if parsed is not None and not isinstance(parsed, ast.Constant):
                    return self._annotation_classes(mod, parsed, owner)
            if text and "." not in text:
                text = mod.bindings.get(text, text)
            return self._named_classes(mod, text)
        if isinstance(node, ast.Name):
            return self._named_classes(mod, mod.bindings.get(node.id, node.id))
        if isinstance(node, ast.Attribute):
            return self._named_classes(mod, _dotted(mod, node))
        if isinstance(node, ast.Subscript):
            if _typing_wrapper(node.value):
                return self._annotation_classes(mod, node.slice, owner)
            found = self._annotation_classes(mod, node.value, owner)
            return found or self._annotation_classes(mod, node.slice, owner)
        if isinstance(node, ast.Tuple):
            found: set[str] = set()
            for elt in node.elts:
                found |= self._annotation_classes(mod, elt, owner)
            return found
        if isinstance(node, ast.BinOp):
            return self._annotation_classes(mod, node.left, owner) | self._annotation_classes(
                mod, node.right, owner
            )
        return set()

    def _named_classes(self, mod: _Mod, dotted: str) -> set[str]:
        dotted = self._resolve_alias(dotted) if dotted else ""
        if not dotted:
            return set()
        defined = self._define(dotted)
        if defined and defined[2] == "class":
            qual = self._class_qual(defined[0], defined[1]) if defined[1] is not None else dotted
            return {qual or dotted}
        if dotted in mod.classes:
            return {dotted}
        return set()

    def _expr_classes(
        self,
        mod: _Mod,
        expr: ast.AST | None,
        fn: ast.AST | None,
        owner: str,
        depth: int,
    ) -> set[str]:
        if expr is None or depth > _DEPTH:
            return set()
        if isinstance(expr, ast.Await):
            return self._expr_classes(mod, expr.value, fn, owner, depth + 1)
        if isinstance(expr, ast.Name):
            if expr.id in {"self", "cls"} and owner:
                return {owner}
            if fn is not None and _is_param(fn, expr.id):
                return self._param_classes(mod, fn, expr.id, owner, depth)
            if fn is not None:
                local = _function_assign(fn, expr.id)
                if local is not None:
                    return self._expr_classes(mod, local, fn, owner, depth + 1)
            named = self._named_classes(mod, mod.bindings.get(expr.id, expr.id))
            if named or not owner:
                return named
            return self._owner_param_classes(mod, owner, expr.id, depth)
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in {"self", "cls"} and owner:
                typed = self.instance_attribute(owner, expr.attr)
                return {typed} if typed else set()
            return self._named_classes(mod, _dotted(mod, expr))
        if isinstance(expr, ast.Call):
            dynamic = _type_call_base(expr)
            if dynamic is not None:
                return self._expr_classes(mod, dynamic, fn, owner, depth + 1)
            if isinstance(expr.func, ast.Name) and expr.func.id in {"self", "cls"} and owner:
                return {owner}
            callee = self._expr_classes(mod, expr.func, fn, owner, depth + 1)
            if callee:
                self._note_constructor_args(mod, expr, fn, owner, callee)
                return callee
            target = ""
            if isinstance(expr.func, ast.Attribute) and isinstance(expr.func.value, ast.Name):
                if expr.func.value.id in {"self", "cls"} and owner:
                    target = f"{owner}.{expr.func.attr}"
            elif isinstance(expr.func, ast.Name):
                target = mod.bindings.get(expr.func.id, "")
            elif isinstance(expr.func, ast.Attribute):
                target = _dotted(mod, expr.func)
            if not target:
                return set()
            returned = set(self.call_return(target))
            if returned:
                return returned
            # A helper may take the concrete class as a keyword and construct
            # it. When the helper's own return is unknown, a Name/Attribute
            # keyword that refers to a class (possibly via a local assign) is
            # that result.
            for keyword in expr.keywords:
                if keyword.arg is None:
                    continue
                if not self._keyword_class_ref(mod, keyword.value, fn):
                    continue
                found = self._expr_classes(mod, keyword.value, fn, owner, depth + 1)
                if found:
                    return found
            return set()
        if isinstance(expr, ast.IfExp):
            return self._expr_classes(mod, expr.body, fn, owner, depth + 1) | self._expr_classes(
                mod, expr.orelse, fn, owner, depth + 1
            )
        if isinstance(expr, ast.Subscript):
            found: set[str] = set()
            for klass in self._expr_classes(mod, expr.value, fn, owner, depth + 1):
                found |= set(self.call_return(f"{klass}.__getitem__"))
            if not found and fn is not None and isinstance(expr.value, ast.Name):
                for value in _subscript_assigns(fn, expr.value.id):
                    found |= self._expr_classes(mod, value, fn, owner, depth + 1)
            return found
        return set()

    def _param_classes(
        self,
        mod: _Mod,
        fn: ast.AST,
        name: str,
        owner: str,
        depth: int,
    ) -> set[str]:
        ann = _param_annotation(fn, name)
        if ann is not None:
            found = self._annotation_classes(mod, ann, owner)
            if found:
                return found
        default = _param_default(fn, name)
        if default is not None and not _is_none(default):
            return self._expr_classes(mod, default, fn, owner, depth + 1)
        typed = self._ctor_args.get((owner, name), "") if owner else ""
        if typed:
            return {typed}
        return set()

    def _owner_param_classes(self, mod: _Mod, owner: str, name: str, depth: int) -> set[str]:
        if depth > _DEPTH:
            return set()
        for method in ("__init__", "__new__"):
            fn = mod.functions.get(f"{owner}.{method}")
            if fn is None or not _is_param(fn, name):
                continue
            return self._param_classes(mod, fn, name, owner, depth)
        for qual, fn in mod.functions.items():
            if qual.rsplit(".", 1)[0] != owner or not _is_param(fn, name):
                continue
            found = self._param_classes(mod, fn, name, owner, depth)
            if found:
                return found
        return set()

    def _note_constructor_args(
        self,
        mod: _Mod,
        expr: ast.Call,
        fn: ast.AST | None,
        owner: str,
        classes: set[str],
    ) -> None:
        for klass in classes:
            defined = self._define(klass)
            if not defined or defined[2] != "class" or defined[1] is None:
                continue
            init = defined[0].functions.get(f"{klass}.__init__")
            if not isinstance(init, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [
                arg.arg
                for arg in (*init.args.posonlyargs, *init.args.args)
                if arg.arg not in {"self", "cls"}
            ]
            for name, arg in zip(params, expr.args):
                typed = self._simple_class(mod, arg, fn, owner)
                if typed and (klass, name) not in self._ctor_args:
                    self._ctor_args[(klass, name)] = typed
                    self._attribute_types.pop((klass, name), None)

    def _simple_class(self, mod: _Mod, arg: ast.AST, fn: ast.AST | None, owner: str) -> str:
        if isinstance(arg, ast.Name) and arg.id in {"self", "cls"} and owner:
            return owner
        if isinstance(arg, ast.Name):
            named = self._named_classes(mod, mod.bindings.get(arg.id, arg.id))
            if len(named) == 1:
                return next(iter(named))
            if fn is not None and _is_param(fn, arg.id):
                found = self._param_classes(mod, fn, arg.id, owner, 0)
                if len(found) == 1:
                    return next(iter(found))
        return ""

    def _keyword_class_ref(
        self, mod: _Mod, expr: ast.AST, fn: ast.AST | None
    ) -> bool:
        """True when a keyword value is a class, including ``name = Class``."""
        cur: ast.AST = expr
        seen: set[str] = set()
        while isinstance(cur, ast.Name) and fn is not None and cur.id not in seen:
            seen.add(cur.id)
            if cur.id in {"self", "cls"}:
                return False
            local = _function_assign(fn, cur.id)
            if local is None:
                break
            cur = local
        target = ""
        if isinstance(cur, ast.Name):
            if cur.id in {"self", "cls"}:
                return False
            target = mod.bindings.get(cur.id, cur.id)
        elif isinstance(cur, ast.Attribute):
            target = _dotted(mod, cur)
        if not target:
            return False
        defined = self._define(target)
        return bool(defined and defined[2] == "class")

    def _modules(self, top: str) -> dict[str, _Mod]:
        top = (top or "").strip()
        if not top:
            return {}
        if top in self._facts and top not in self._dist:
            # Facts-only hit: no AST modules, queries answer from ``_facts``.
            self._dist[top] = None
            return {}
        cached = self._dist.get(top)
        if cached is not None:
            return cached
        if top in self._dist:
            return {}
        pin = self._pins.get(top, "")
        from conduit.analysis_cache import cache_enabled

        wheel = _resolve_wheel(top, pin)
        if wheel is not None and cache_enabled():
            digest = file_sha256(wheel)
            hit = read_json("libtypes", digest)
            facts = _DistFacts.from_json(hit)
            if facts is not None:
                self._facts[top] = facts
                self._dist[top] = None
                return {}
        root = _unpack(wheel, top) if wheel is not None else _distribution_root(top, pin)
        if root is None:
            self._dist[top] = None
            return {}
        mods = _parse_distribution(root, top)
        self._dist[top] = mods or None
        if mods and wheel is not None:
            from conduit.analysis_cache import cache_enabled

            # Full fact extraction walks every public symbol. Keep it for small
            # wheels (stubs, thin clients). Large SDKs still benefit from the
            # load_api / diff caches; re-parsing their AST once per process is
            # cheaper than an eager whole-surface walk.
            if cache_enabled() and len(mods) <= 80:
                facts = self._extract_facts(top, mods)
                self._facts[top] = facts
                write_json("libtypes", file_sha256(wheel), facts.to_json())
        return mods

    def _extract_facts(self, top: str, mods: dict[str, _Mod]) -> _DistFacts:
        """Walk the parsed distribution once and record answers for the cache."""
        self._extracting = True
        try:
            returns: dict[str, list[str]] = {}
            attribute_types: dict[str, str] = {}
            delegated: dict[str, list[str]] = {}
            init_params: dict[str, list[str]] = {}
            members: dict[str, str] = {}
            for mod in mods.values():
                for qual in list(mod.functions):
                    if not _public_qual(qual):
                        continue
                    returns[qual] = list(self.call_return(qual))
                    delegated[qual] = list(self.delegated_calls(qual))
                for qual in list(mod.classes):
                    if not _public_qual(qual):
                        continue
                    init_params[qual] = list(self.init_params(qual))
                    returns[qual] = list(self.call_return(qual))
                    for name in self._all_member_names(qual, mods):
                        if name.startswith("_") and name not in {"__init__", "__getattr__", "__getitem__"}:
                            continue
                        owner = self.member_owner(qual, name)
                        if owner:
                            members[f"{qual}.{name}"] = owner
                        typed = self.instance_attribute(qual, name)
                        if typed:
                            attribute_types[f"{qual}.{name}"] = typed
            return _DistFacts(
                returns=returns,
                attribute_types=attribute_types,
                delegated=delegated,
                init_params=init_params,
                members=members,
            )
        finally:
            self._extracting = False

    def _all_member_names(self, qual: str, mods: dict[str, _Mod]) -> list[str]:
        """Method and data names on ``qual``, including bases."""
        names: list[str] = []
        seen: set[str] = set()
        stack = [qual]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            mod = None
            node = None
            for candidate in mods.values():
                item = candidate.classes.get(current)
                if item is not None:
                    mod = candidate
                    node = item
                    break
            if mod is None or node is None:
                continue
            names.extend(_class_member_names(mod, current))
            stack.extend(self._bases(mod, node))
        return list(dict.fromkeys(names))

    def _modules_of(self, module: str) -> dict[str, _Mod]:
        return self._modules(module.split(".", 1)[0])


_TYPING_WRAPPERS = frozenset(
    {
        "optional",
        "union",
        "list",
        "set",
        "tuple",
        "dict",
        "sequence",
        "mapping",
        "iterable",
        "iterator",
        "collection",
        "callable",
        "annotated",
        "classvar",
        "final",
        "literal",
        "type",
        "frozenset",
    }
)


def _typing_wrapper(node: ast.AST) -> bool:
    name = ""
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    return name.lower() in _TYPING_WRAPPERS


def _call_nodes(node: ast.AST) -> list[ast.Call]:
    """Calls in a function body, not in a nested function or class."""
    found: list[ast.Call] = []
    stack = list(getattr(node, "body", []) or [])
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call):
            found.append(child)
        stack.extend(ast.iter_child_nodes(child))
    return found


def _called_qual(mod: _Mod, call: ast.Call, owner: str) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return mod.bindings.get(func.id, "")
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in {"self", "cls"} and owner:
            return f"{owner}.{func.attr}"
        base = mod.bindings.get(func.value.id, "")
        return f"{base}.{func.attr}" if base else ""
    if isinstance(func, ast.Attribute):
        return _dotted(mod, func)
    return ""


def _clean(qualname: str) -> str:
    return ".".join(part for part in (qualname or "").split(".") if part)


def _owner_method(qualname: str) -> tuple[str, str]:
    if "." not in qualname:
        return "", ""
    owner, _, method = qualname.rpartition(".")
    return owner, method


def _dotted(mod: _Mod, node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return mod.bindings.get(node.id) or node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(mod, node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_param(fn: ast.AST, name: str) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(
        arg.arg == name
        for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
    )


def _param_default(fn: ast.AST, name: str) -> ast.expr | None:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    args = [*fn.args.posonlyargs, *fn.args.args]
    defaults = list(fn.args.defaults)
    offset = len(args) - len(defaults)
    for index, arg in enumerate(args):
        if arg.arg == name and index >= offset:
            return defaults[index - offset]
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        if arg.arg == name:
            return default
    return None


def _param_annotation(fn: ast.AST, name: str) -> ast.expr | None:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs):
        if arg.arg == name:
            return arg.annotation
    return None


def _return_exprs(node: ast.AST) -> list[ast.expr]:
    found: list[ast.expr] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and child.value is not None:
            found.append(child.value)
        elif isinstance(child, ast.Yield) and child.value is not None and _context_manager(node):
            found.append(child.value)
    return found


def _wrapper_returns_self(fn: ast.AST) -> bool:
    """True when ``fn`` or a wrapper nested in it returns the instance.

    The instance is a ``self`` parameter, or ``args[0]`` when the wrapper is
    ``def generate(fn, *args)`` and assigns ``self = args[0]._clone()``.
    """
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    nested = [
        child
        for child in fn.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return any(_function_returns_instance(item) for item in (fn, *nested))


def _function_returns_instance(fn: ast.AST) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    names: set[str] = set()
    if _is_param(fn, "self"):
        names.add("self")
    vararg = fn.args.vararg.arg if fn.args.vararg is not None else ""
    # ``def generate(fn, *args)`` — the wrapped method's instance is args[0].
    wrapped = bool(fn.args.args) and bool(vararg)

    def is_instance(expr: ast.AST) -> bool:
        if isinstance(expr, ast.Name) and expr.id in names:
            return True
        if (
            wrapped
            and isinstance(expr, ast.Subscript)
            and isinstance(expr.value, ast.Name)
            and expr.value.id == vararg
            and _slice_index(expr.slice) == 0
        ):
            return True
        if isinstance(expr, ast.Attribute):
            return is_instance(expr.value)
        if isinstance(expr, ast.Call):
            func = expr.func
            target = func.value if isinstance(func, ast.Attribute) else func
            return is_instance(target)
        return False

    stack = list(reversed(list(fn.body)))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and node.value is not None and is_instance(node.value):
                    names.add(target.id)
            continue
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and is_instance(node.value)
        ):
            names.add(node.target.id)
            continue
        if isinstance(node, ast.Return) and node.value is not None and is_instance(node.value):
            return True
        children = [
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
        ]
        stack.extend(reversed(children))
    return False


def _function_assign(fn: ast.AST, name: str) -> ast.expr | None:
    """Last assignment to ``name`` in ``fn``, ignoring nested functions."""
    found: ast.expr | None = None
    stack = list(reversed(list(getattr(fn, "body", ()))))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        value: ast.expr | None = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if value is not None:
            for target in targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found = value
            continue
        children = [
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
        ]
        stack.extend(reversed(children))
    return found


def _subscript_assigns(fn: ast.AST, name: str) -> list[ast.expr]:
    """Values written to ``name[...]`` in ``fn``, ignoring nested functions."""
    found: list[ast.expr] = []
    stack = list(reversed(list(getattr(fn, "body", ()))))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == name
                ):
                    found.append(node.value)
            continue
        children = [
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
        ]
        stack.extend(reversed(children))
    return found


def _slice_index(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _context_manager(node: ast.AST) -> bool:
    for deco in getattr(node, "decorator_list", []) or []:
        name = deco.attr if isinstance(deco, ast.Attribute) else deco.id if isinstance(deco, ast.Name) else ""
        if name == "contextmanager":
            return True
    return False


def _type_call_base(expr: ast.Call) -> ast.AST | None:
    """``type(name, (Base,), {})`` is a subclass of ``Base``."""
    func = expr.func
    if not isinstance(func, ast.Name) or func.id != "type" or len(expr.args) < 2:
        return None
    bases = expr.args[1]
    if isinstance(bases, ast.Tuple) and bases.elts:
        return bases.elts[0]
    return None


def _distribution_root(top: str, pin: str) -> Path | None:
    installed = _installed_root(top)
    if installed is not None:
        return installed
    wheel = _resolve_wheel(top, pin)
    if wheel is None:
        return None
    return _unpack(wheel, top)


def _resolve_wheel(top: str, pin: str) -> Path | None:
    """Locate a wheel/sdist for ``top`` without unpacking it."""
    cached = _cached_wheel(top, pin)
    if cached is not None:
        return cached
    version = pin if _exact_version(pin) else ""
    if not version:
        return None
    return _download(top, version)


def _public_qual(qual: str) -> bool:
    parts = [part for part in qual.split(".") if part]
    if not parts:
        return False
    # Private modules stay out of the cache; dunder methods on public classes stay in.
    return all(not part.startswith("_") for part in parts[:-1]) and (
        not parts[-1].startswith("_") or parts[-1].startswith("__")
    )


def _class_member_names(mod: _Mod, qual: str) -> list[str]:
    node = mod.classes.get(qual)
    if node is None:
        return []
    names: list[str] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(child.name)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            names.append(child.target.id)
    for (owner, name) in mod.self_assigns:
        if owner == qual:
            names.append(name)
    for (owner, name) in mod.class_ann:
        if owner == qual:
            names.append(name)
    return list(dict.fromkeys(names))


def _exact_version(pin: str) -> bool:
    text = (pin or "").strip()
    if not text or any(op in text for op in "<>!,~^*"):
        return False
    return text[0].isdigit()


def _installed_root(top: str) -> Path | None:
    try:
        import importlib.util

        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    origin = Path(spec.origin)
    if origin.name == "__init__.py":
        return origin.parent.parent
    return origin.parent


def _cached_wheel(top: str, pin: str) -> Path | None:
    key = top.lower().replace("-", "_")
    found: list[Path] = []
    for directory in _wheel_dirs():
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            name = path.name.lower()
            prefix = key + "-"
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix) :]
            if not rest or not rest[0].isdigit():
                continue
            if path.suffix == ".whl" or name.endswith(".tar.gz"):
                found.append(path)
    if not found:
        return None
    pin_l = pin.lower() if _exact_version(pin) else ""

    def rank(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        exact = 0 if pin_l and (f"-{pin_l}-" in name or name.endswith(f"-{pin_l}.tar.gz")) else 1
        if "cp27" in name or "py2.7" in name or "py2-" in name:
            kind = 3
        elif name.endswith(".tar.gz") or "py3-none-any" in name or "py2.py3-none-any" in name:
            kind = 0
        else:
            kind = 1
        return (exact, kind, name)

    if pin_l:
        exact = [path for path in found if rank(path)[0] == 0]
        if exact:
            return sorted(exact, key=rank)[0]
    return sorted(found, key=rank)[0]


def _download(top: str, version: str) -> Path | None:
    dest_dir = _libtypes_cache() / "wheels"
    dest_dir.mkdir(parents=True, exist_ok=True)
    meta_url = f"https://pypi.org/pypi/{top}/{version}/json"
    try:
        with urlopen(meta_url, timeout=30) as handle:
            payload = json.loads(handle.read().decode("utf-8"))
    except Exception:
        return None
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        return None
    chosen = _pick_url(urls)
    if chosen is None:
        return None
    filename, file_url, size = chosen
    if size and size > _MAX_DOWNLOAD:
        return None
    dest = dest_dir / filename
    if dest.is_file():
        return dest
    try:
        with urlopen(file_url, timeout=60) as handle:
            blob = handle.read(_MAX_DOWNLOAD + 1)
    except Exception:
        return None
    if len(blob) > _MAX_DOWNLOAD:
        return None
    dest.write_bytes(blob)
    return dest


def _pick_url(urls: list[object]) -> tuple[str, str, int] | None:
    wheels: list[tuple[str, str, int]] = []
    sdists: list[tuple[str, str, int]] = []
    for item in urls:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "")
        url = str(item.get("url") or "")
        size = int(item.get("size") or 0)
        kind = str(item.get("packagetype") or "")
        if not name or not url:
            continue
        if kind == "bdist_wheel" and "py3" in name or kind == "bdist_wheel" and "py2.py3" in name:
            wheels.append((name, url, size))
        elif kind == "sdist":
            sdists.append((name, url, size))
    if wheels:
        wheels.sort(key=lambda item: (0 if "none-any" in item[0] else 1, item[2] or 0))
        return wheels[0]
    return sdists[0] if sdists else None


def _unpack(archive: Path, top: str) -> Path | None:
    dest = _libtypes_cache() / "src" / f"{top}-{archive.name}"
    marker = dest / ".ok"
    if marker.is_file():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(dest)
        elif archive.name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(dest)
        else:
            return None
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    marker.write_text("ok", encoding="utf-8")
    return dest


def _parse_distribution(root: Path, top: str) -> dict[str, _Mod]:
    import_root = _import_root(root, top)
    if import_root is None:
        return {}
    mods: dict[str, _Mod] = {}
    files = []
    for path in import_root.rglob("*"):
        if path.suffix not in {".py", ".pyi"}:
            continue
        if any(part in {"test", "tests", "testing", "__pycache__"} for part in path.parts):
            continue
        files.append(path)
        if len(files) > _MAX_FILES:
            break
    for path in files:
        module = _module_name(import_root, path)
        if not module:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        package = module if path.name.startswith("__init__") else module.rsplit(".", 1)[0]
        mod = _mod_from_tree(module, package, tree)
        key = module if path.suffix == ".py" else module
        if path.suffix == ".pyi" and module in mods:
            mods[module + ".pyi-overlay"] = mod
            continue
        if path.suffix == ".pyi":
            mods[module] = mod
            mods[module + ".pyi-overlay"] = mod
            continue
        mods[module] = mod
    _expand_star_imports(mods)
    return mods


def _expand_star_imports(mods: dict[str, _Mod]) -> None:
    """Copy public names from ``from pkg import *`` into the importing module.

    Distributions often re-export helpers this way. Without the copy,
    ``pkg.connect`` does not resolve to the function defined in a submodule.
    """
    for _ in range(4):
        changed = False
        for mod in mods.values():
            package = mod.name if mod.name in mods else mod.name.rsplit(".", 1)[0]
            for node in _module_body(mod.tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                for alias in node.names:
                    if alias.name != "*":
                        continue
                    base = _abs_module(package, node.level or 0, node.module or "")
                    source = mods.get(base)
                    if source is None:
                        continue
                    for name, target in _public_exports(source).items():
                        if name not in mod.bindings:
                            mod.bindings[name] = target
                            changed = True
        if not changed:
            break


def _module_body(tree: ast.AST) -> list[ast.AST]:
    found: list[ast.AST] = []
    stack = list(getattr(tree, "body", []) or [])
    while stack:
        node = stack.pop(0)
        if isinstance(node, ast.If):
            stack[0:0] = list(node.body) + list(node.orelse)
            continue
        found.append(node)
    return found


def _public_exports(mod: _Mod) -> dict[str, str]:
    """Names a star-import should copy from ``mod``."""
    exported: dict[str, str] = {}
    all_names = _dunder_all(mod)
    if all_names is not None:
        for name in all_names:
            target = mod.bindings.get(name, f"{mod.name}.{name}")
            if name in mod.bindings or f"{mod.name}.{name}" in mod.functions or f"{mod.name}.{name}" in mod.classes:
                exported[name] = target if name in mod.bindings else f"{mod.name}.{name}"
        return exported
    for name, target in mod.bindings.items():
        if not name.startswith("_"):
            exported[name] = target
    return exported


def _dunder_all(mod: _Mod) -> list[str] | None:
    for node in getattr(mod.tree, "body", []) or []:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            return None
        names = [
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        return names
    return None


def _import_root(root: Path, top: str) -> Path | None:
    direct = root / top
    if (direct / "__init__.py").is_file() or (direct / "__init__.pyi").is_file():
        return root
    for child in root.rglob("__init__.py"):
        if child.parent.name == top:
            return child.parent.parent
    for child in root.rglob("__init__.pyi"):
        if child.parent.name == top:
            return child.parent.parent
    return None


def _module_name(root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ""
    parts = list(rel.parts)
    if parts[-1] in {"__init__.py", "__init__.pyi"}:
        parts = parts[:-1]
    else:
        parts[-1] = Path(parts[-1]).stem
        if parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
    return ".".join(part for part in parts if part)


def _mod_from_tree(module: str, package: str, tree: ast.AST) -> _Mod:
    bindings: dict[str, str] = {}
    classes: dict[str, ast.ClassDef] = {}
    functions: dict[str, ast.AST] = {}
    self_assigns: dict[tuple[str, str], list[ast.expr]] = {}
    class_ann: dict[tuple[str, str], ast.expr] = {}
    for node in getattr(tree, "body", []) or []:
        _collect(
            module,
            package,
            node,
            bindings,
            classes,
            functions,
            self_assigns,
            class_ann,
            class_qual="",
        )
    return _Mod(module, tree, bindings, classes, functions, self_assigns, class_ann)


def _collect(
    module: str,
    package: str,
    node: ast.AST,
    bindings: dict[str, str],
    classes: dict[str, ast.ClassDef],
    functions: dict[str, ast.AST],
    self_assigns: dict[tuple[str, str], list[ast.expr]],
    class_ann: dict[tuple[str, str], ast.expr],
    class_qual: str,
) -> None:
    if isinstance(node, ast.If):
        for child in list(node.body) + list(node.orelse):
            _collect(module, package, child, bindings, classes, functions, self_assigns, class_ann, class_qual)
        return
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            bindings[local] = alias.name if alias.asname else alias.name.split(".")[0]
        return
    if isinstance(node, ast.ImportFrom):
        base = _abs_module(package, node.level or 0, node.module or "")
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            bindings[local] = f"{base}.{alias.name}" if base else alias.name
        return
    if isinstance(node, ast.ClassDef):
        qual = f"{module}.{node.name}"
        classes[qual] = node
        bindings.setdefault(node.name, qual)
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.annotation:
                class_ann[(qual, child.target.id)] = child.annotation
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[f"{qual}.{child.name}"] = child
                _self_assigns(qual, child, self_assigns)
            elif isinstance(child, ast.ClassDef):
                _collect(module, package, child, bindings, classes, functions, self_assigns, class_ann, qual)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        qual = f"{module}.{node.name}"
        functions[qual] = node
        bindings.setdefault(node.name, qual)


def _self_assigns(class_qual: str, fn: ast.AST, found: dict[tuple[str, str], list[ast.expr]]) -> None:
    for node in ast.walk(fn):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in {"self", "cls"}
            ):
                found.setdefault((class_qual, target.attr), []).append(value)


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

