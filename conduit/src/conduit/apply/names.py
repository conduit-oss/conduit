"""Target-version SDK names: ground the model, then reject invented ones.

Before a rewrite, ``ground_candidates`` ranks a short list of real public names
from the target wheel (modules, classes, functions, methods) using the same
token / parameter overlap signals as impact candidates — no vendor tables.

After a rewrite, ``name_breaks`` checks new or used SDK imports and
module-rooted attribute chains against that wheel. One corrective retry is
allowed; a second miss stays needs-human and is never verified.

Resolution prefers importing the installed/extracted target package the way
Python does (``getattr`` along the chain, including lazy ``__getattr__``
modules). Static AST parsing is a fallback; when a module uses lazy export
tables, static absence is treated as **unknown**, never as invented-missing.

Surface extraction reuses ``outside_in.ast_checks`` single-pass walkers so this
does not add a parallel AST walker over the release modules.
"""

from __future__ import annotations

import ast
import builtins
import json
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from conduit.apply.typecheck import _module_binds
from conduit.impact.candidates import pick_candidates, symbol_tokens
from conduit.outside_in.ast_checks import AstCheck, run_checks

#: Cap for prompt grounding and for "nearby names" in findings.
MAX_GROUND_CANDIDATES = 16
_MAX_NEARBY = 12
_MAX_SIGNATURE_CHARS = 160
#: Distinctive phrase so the apply engine can hand off after one name retry.
DOES_NOT_EXIST = "does not exist in"
#: Phrase for names the rewrite left used but unbound (pyflakes-style).
NEWLY_UNDEFINED = "is newly undefined after the rewrite"
_RUNTIME_TIMEOUT = 8.0


def ground_candidates(
    modules: dict[str, ast.Module],
    *,
    package: str,
    hints: list[str],
    old_params: list[str] | None = None,
    call_kwargs: list[str] | None = None,
    limit: int = MAX_GROUND_CANDIDATES,
    wheel: Path | None = None,
    python: str | None = None,
) -> list[dict[str, str]]:
    """Short list of real target-version names near ``hints``.

    Builds the public surface once from ``modules``, then ranks with
    ``pick_candidates`` (token similarity, leaf match, parameter overlap).
    Runtime-confirmed hint names (lazy exports) are promoted onto the surface.
    """
    top = _top(package)
    if not top:
        return []
    surface = public_surface(modules, top) if modules else {}
    for hint in hints:
        dotted = _hint_to_dotted(hint, top)
        if not dotted or dotted in surface:
            continue
        status, _ = resolve_name(
            dotted, top, modules, wheel=wheel, python=python
        )
        if status == "ok":
            surface[dotted] = f"{dotted}()"
    if not surface:
        return []
    params = {
        name: _params_from_signature(signature) for name, signature in surface.items()
    }
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for hint in hints:
        hint = (hint or "").strip()
        if not hint:
            continue
        for row in pick_candidates(
            hint,
            surface,
            added_params=params,
            old_params=old_params,
            call_kwargs=call_kwargs,
            limit=limit,
        ):
            symbol = row.get("symbol") or ""
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def _hint_to_dotted(hint: str, top: str) -> str:
    text = (hint or "").strip()
    if not text:
        return ""
    if text == top or text.startswith(top + "."):
        return text
    return ""


def resolve_name(
    dotted: str,
    package: str,
    modules: dict[str, ast.Module],
    *,
    wheel: Path | None = None,
    python: str | None = None,
) -> tuple[str, object | None]:
    """Walk ``dotted`` the way Python would.

    Returns ``(status, leaf)`` where status is ``"ok"``, ``"missing"``, or
    ``"unknown"``. Prefer a real import + ``getattr`` (including lazy
    ``__getattr__``). Static AST is a fallback; lazy export tables yield
    ``unknown``, never invented-missing.
    """
    top = _top(package)
    if not dotted or not top:
        return "missing", None
    if not (dotted == top or dotted.startswith(top + ".")):
        return "missing", None
    runtime = _runtime_resolve(dotted, top, wheel=wheel, python=python)
    if runtime is not None:
        return runtime, None
    return _static_resolve(dotted, top, modules), None


@lru_cache(maxsize=256)
def _runtime_resolve_cached(
    dotted: str,
    package: str,
    wheel_key: str,
    python: str,
) -> str | None:
    wheel = Path(wheel_key) if wheel_key else None
    return _runtime_resolve_uncached(dotted, package, wheel=wheel, python=python)


def _runtime_resolve(
    dotted: str,
    package: str,
    *,
    wheel: Path | None,
    python: str | None,
) -> str | None:
    py = python or sys.executable
    key = str(wheel) if wheel is not None else ""
    try:
        return _runtime_resolve_cached(dotted, package, key, py)
    except (OSError, TypeError, ValueError):
        return _runtime_resolve_uncached(dotted, package, wheel=wheel, python=py)


def _runtime_resolve_uncached(
    dotted: str,
    package: str,
    *,
    wheel: Path | None,
    python: str | None,
) -> str | None:
    """Import the package and walk attributes. ``None`` if the probe cannot run."""
    script = (
        "import importlib, json, sys\n"
        f"pkg = {package!r}\n"
        f"dotted = {dotted!r}\n"
        "try:\n"
        "    mod = importlib.import_module(pkg)\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'status': 'probe_failed', 'err': type(exc).__name__}))\n"
        "    raise SystemExit(0)\n"
        "cur = mod\n"
        "parts = dotted.split('.')[1:]\n"
        "for part in parts:\n"
        "    try:\n"
        "        cur = getattr(cur, part)\n"
        "    except AttributeError:\n"
        "        print(json.dumps({'status': 'missing'}))\n"
        "        raise SystemExit(0)\n"
        "    except Exception as exc:\n"
        "        print(json.dumps({'status': 'unknown', 'err': type(exc).__name__}))\n"
        "        raise SystemExit(0)\n"
        "print(json.dumps({'status': 'ok'}))\n"
    )
    cmd_python = python or sys.executable
    try:
        if wheel is not None:
            path = Path(wheel)
            if path.is_dir():
                return _run_name_probe(cmd_python, script, env_pythonpath=str(path))
            if path.is_file():
                with tempfile.TemporaryDirectory(prefix="conduit-name-") as tmp:
                    extract = Path(tmp)
                    with zipfile.ZipFile(path) as zf:
                        zf.extractall(extract)
                    return _run_name_probe(cmd_python, script, env_pythonpath=str(extract))
        return _run_name_probe(cmd_python, script, env_pythonpath=None)
    except OSError:
        return None


def _runtime_kind(
    dotted: str,
    package: str,
    *,
    wheel: Path | None,
    python: str | None = None,
) -> str | None:
    """Kind of ``dotted`` from an import + ``getattr`` of ``wheel``.

    ``exception`` is a ``BaseException`` subclass. ``class`` is any other class.
    ``callable`` is a function (not a class). ``module`` is a module. ``None``
    when the probe cannot run. A wheel is required so the ambient install is
    not consulted.
    """
    if wheel is None or not dotted or not package:
        return None
    py = python or sys.executable
    key = str(wheel)
    try:
        return _runtime_kind_cached(dotted, package, key, py)
    except (OSError, TypeError, ValueError):
        return _runtime_kind_uncached(dotted, package, wheel=wheel, python=py)


@lru_cache(maxsize=256)
def _runtime_kind_cached(dotted: str, package: str, wheel_key: str, python: str) -> str | None:
    wheel = Path(wheel_key) if wheel_key else None
    return _runtime_kind_uncached(dotted, package, wheel=wheel, python=python)


def _runtime_kind_uncached(
    dotted: str,
    package: str,
    *,
    wheel: Path | None,
    python: str | None,
) -> str | None:
    if wheel is None or not package:
        return None
    script = (
        "import importlib, inspect, json\n"
        f"dotted = {dotted!r}\n"
        "parent, _, leaf = dotted.rpartition('.')\n"
        "try:\n"
        "    mod = importlib.import_module(parent or dotted)\n"
        "    obj = getattr(mod, leaf) if leaf else mod\n"
        "except Exception:\n"
        "    print(json.dumps({'kind': ''}))\n"
        "    raise SystemExit(0)\n"
        "if inspect.ismodule(obj):\n"
        "    kind = 'module'\n"
        "elif isinstance(obj, type):\n"
        "    kind = 'exc_type' if issubclass(obj, BaseException) else 'class'\n"
        "elif callable(obj):\n"
        "    kind = 'callable'\n"
        "else:\n"
        "    kind = 'value'\n"
        "print(json.dumps({'kind': kind}))\n"
    )
    cmd_python = python or sys.executable
    path = Path(wheel)
    try:
        if path.is_dir():
            return _read_kind_probe(cmd_python, script, str(path))
        if path.is_file():
            with tempfile.TemporaryDirectory(prefix="conduit-kind-") as tmp:
                extract = Path(tmp)
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(extract)
                return _read_kind_probe(cmd_python, script, str(extract))
    except OSError:
        return None
    return None


def _read_kind_probe(python: str, script: str, pythonpath: str) -> str | None:
    import os

    env = os.environ.copy()
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = pythonpath + (os.pathsep + prev if prev else "")
    try:
        proc = subprocess.run(
            [python, "-c", script],
            capture_output=True,
            text=True,
            timeout=_RUNTIME_TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    kind = str(payload.get("kind") or "")
    if kind in {"exc_type", "class", "callable", "module", "value"}:
        return kind
    return None


def _run_name_probe(
    python: str,
    script: str,
    *,
    env_pythonpath: str | None,
) -> str | None:
    import os

    env = os.environ.copy()
    if env_pythonpath:
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = env_pythonpath + (os.pathsep + prev if prev else "")
    try:
        proc = subprocess.run(
            [python, "-c", script],
            capture_output=True,
            text=True,
            timeout=_RUNTIME_TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    status = payload.get("status")
    if status == "ok":
        return "ok"
    if status == "missing":
        return "missing"
    if status == "unknown":
        return "unknown"
    # probe_failed: package not importable here → fall back to static
    return None


def _static_resolve(
    dotted: str,
    top: str,
    modules: dict[str, ast.Module],
) -> str:
    if not modules:
        return "unknown"
    parts = dotted.split(".")
    for depth in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:depth])
        tree = modules.get(mod_name)
        if tree is None:
            continue
        rest = parts[depth:]
        if not rest:
            return "ok"
        if _module_is_lazy(tree):
            return "unknown"
        current = mod_name
        node: object | None = tree
        for segment in rest:
            child = f"{current}.{segment}"
            if isinstance(node, ast.Module) and _module_is_lazy(node):
                return "unknown"
            if _module_present(modules, child):
                current = child
                node = modules.get(child) or ast.Module(body=[], type_ignores=[])
                continue
            nxt = _attr_on(node, segment, modules, current)
            if nxt is None:
                return "missing"
            node = nxt
            current = child
        return "ok"
    if _module_present(modules, dotted):
        return "ok"
    root = modules.get(top)
    if root is not None and _module_is_lazy(root):
        return "unknown"
    return "missing"


def _builtin_exception_names() -> frozenset[str]:
    """Builtin exception class names. Not a list of product APIs."""
    found: set[str] = set()
    for name in dir(builtins):
        obj = getattr(builtins, name, None)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            found.add(name)
    return frozenset(found)


_BUILTIN_EXCEPTIONS = _builtin_exception_names()


def _static_kind(dotted: str, top: str, modules: dict[str, ast.Module]) -> str | None:
    """Kind of ``dotted`` from the wheel's AST, or None when it cannot be told.

    A class whose bases cannot be classified is None: it might still be an
    exception. A class with no bases is a class. A ``BaseException`` subclass
    is an exception, not a plain class.
    """
    node, module = _static_defining_node(dotted, top, modules)
    if node is None:
        return None
    if isinstance(node, ast.Module):
        return "module"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "callable"
    if isinstance(node, ast.ClassDef):
        flag = _class_is_exception(node, modules, module, set())
        if flag is None:
            return None
        return "exc_type" if flag else "class"
    return None


def _static_defining_node(
    dotted: str,
    top: str,
    modules: dict[str, ast.Module],
) -> tuple[ast.AST | None, str]:
    if not modules or not dotted or not top:
        return None, ""
    if not (dotted == top or dotted.startswith(top + ".")):
        return None, ""
    if dotted in modules:
        return modules[dotted], dotted
    parts = dotted.split(".")
    for depth in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:depth])
        tree = modules.get(mod_name)
        if tree is None:
            continue
        rest = parts[depth:]
        if not rest:
            return tree, mod_name
        if _module_is_lazy(tree):
            return None, ""
        current = mod_name
        node: object | None = tree
        for segment in rest:
            child = f"{current}.{segment}"
            if isinstance(node, ast.Module) and _module_is_lazy(node):
                return None, ""
            if child in modules:
                current = child
                node = modules[child]
                continue
            nxt = _attr_on(node, segment, modules, current)
            if nxt is None:
                return None, ""
            node = nxt
            current = child
        if isinstance(node, ast.AST):
            return node, _owner_module(node, modules) or mod_name
    return None, ""


def _owner_module(node: ast.AST, modules: dict[str, ast.Module]) -> str:
    for name, tree in modules.items():
        if tree is node:
            return name
        for item in tree.body:
            if item is node:
                return name
    return ""


def _class_is_exception(
    node: ast.ClassDef,
    modules: dict[str, ast.Module],
    module: str,
    seen: set[int],
) -> bool | None:
    """True when a base is an exception, False when none are, None if unknown."""
    if id(node) in seen:
        return None
    seen.add(id(node))
    if not node.bases:
        return False
    unknown = False
    for base in node.bases:
        kind = _base_exception_kind(base, modules, module, seen)
        if kind is True:
            return True
        if kind is None:
            unknown = True
    if unknown:
        return None
    return False


def _base_exception_kind(
    base: ast.expr,
    modules: dict[str, ast.Module],
    module: str,
    seen: set[int],
) -> bool | None:
    if isinstance(base, ast.Name):
        if base.id in _BUILTIN_EXCEPTIONS:
            return True
        if base.id in {"object", "type"}:
            return False
        tree = modules.get(module)
        found = _attr_on(tree, base.id, modules, module) if tree is not None else None
        if isinstance(found, ast.ClassDef):
            return _class_is_exception(found, modules, _owner_module(found, modules) or module, seen)
        return None
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        qual = f"{base.value.id}.{base.attr}"
        if qual in modules:
            return False
        for name, tree in modules.items():
            if name == base.value.id or name.endswith("." + base.value.id):
                found = _attr_on(tree, base.attr, modules, name)
                if isinstance(found, ast.ClassDef):
                    return _class_is_exception(
                        found, modules, _owner_module(found, modules) or name, seen
                    )
        return None
    return None


def _module_is_lazy(tree: ast.Module | None) -> bool:
    """True when the module exports via ``__getattr__`` or a lazy-import table."""
    if tree is None:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _lazy_table_name(target.id):
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _lazy_table_name(node.target.id):
                return True
    return False


def _lazy_table_name(name: str) -> bool:
    lower = name.lower()
    if lower in {
        "_lazy_imports",
        "_lazy_import",
        "_import_map",
        "_imports",
        "_module_map",
        "_lazy",
        "__getattr__",
    }:
        return True
    return "lazy" in lower and ("import" in lower or "attr" in lower or "map" in lower)


def newly_undefined_breaks(original: str, patched: str, path: str) -> list[str]:
    """Names used after the rewrite that are unbound (and were bound before).

    Same severity as a name break: one corrective retry, then needs-human.
    Focuses on runtime NameError risk (not annotation-only under
    ``from __future__ import annotations``).
    """
    before = _undefined_names_in(original)
    after = _undefined_names_in(patched)
    findings: list[str] = []
    for name in sorted(after - before):
        line = _first_use_line(patched, name)
        findings.append(f"{path}:{line} name: {name} {NEWLY_UNDEFINED}")
    return findings


def _undefined_names_in(source: str) -> set[str]:
    """Pyflakes-style: Load names that are never bound/imported/builtin.

    When ``from __future__ import annotations`` is present, names that appear
    only in annotations are skipped (they are not evaluated at runtime).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    builtins = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))
    builtins |= {
        "True",
        "False",
        "None",
        "Ellipsis",
        "NotImplemented",
        "print",
        "len",
        "range",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "type",
        "object",
        "isinstance",
        "issubclass",
        "getattr",
        "setattr",
        "hasattr",
        "Exception",
        "BaseException",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "ImportError",
        "RuntimeError",
        "StopIteration",
        "enumerate",
        "zip",
        "map",
        "filter",
        "sorted",
        "reversed",
        "iter",
        "next",
        "open",
        "super",
        "property",
        "classmethod",
        "staticmethod",
        "abs",
        "min",
        "max",
        "sum",
        "any",
        "all",
        "__name__",
        "__file__",
        "__package__",
        "__doc__",
        "__annotations__",
    }
    future_annotations = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    # Collect annotation AST nodes to skip when postponed annotations are on.
    skip_annotation_ids: set[int] = set()
    if future_annotations:
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.annotation is not None:
                skip_annotation_ids.add(id(node.annotation))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
                skip_annotation_ids.add(id(node.returns))
            elif isinstance(node, ast.AnnAssign):
                skip_annotation_ids.add(id(node.annotation))

    bound: set[str] = set()
    used: set[str] = set()

    class V(ast.NodeVisitor):
        def visit(self, node: ast.AST) -> None:
            if id(node) in skip_annotation_ids:
                return
            super().visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                used.add(node.id)

        def visit_arg(self, node: ast.arg) -> None:
            bound.add(node.arg)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".", 1)[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound.add(alias.asname or alias.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            bound.add(node.name)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            bound.add(node.name)
            self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            bound.add(node.name)
            self.generic_visit(node)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                bound.add(node.name)
            self.generic_visit(node)

        def visit_Global(self, node: ast.Global) -> None:
            bound.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            bound.update(node.names)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name):
                bound.add(node.target.id)
            if id(node.annotation) not in skip_annotation_ids:
                self.visit(node.annotation)
            if node.value is not None:
                self.visit(node.value)

    V().visit(tree)
    return {n for n in used if n not in bound and n not in builtins}


def _first_use_line(source: str, name: str) -> int:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 1

    class Finder(ast.NodeVisitor):
        def __init__(self) -> None:
            self.line = 1

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id == name and self.line == 1:
                self.line = getattr(node, "lineno", 1) or 1
            self.generic_visit(node)

    finder = Finder()
    finder.visit(tree)
    return finder.line



def public_surface(modules: dict[str, ast.Module], top: str) -> dict[str, str]:
    """``qualname -> short signature`` for public modules / callables in ``top``."""
    out: dict[str, str] = {}
    for name, tree in modules.items():
        if name != top and not name.startswith(top + "."):
            continue
        out.setdefault(name, f"{name}()")
        check = _PublicSurfaceCheck(name)
        run_checks(tree, [check])
        out.update(check.finish())
        out.update(_module_level_functions(name, tree))
        for child in _child_modules(modules, name):
            dotted = f"{name}.{child}"
            out.setdefault(dotted, f"{dotted}()")
    _alias_reexported_methods(modules, top, out)
    return out


def _alias_reexported_methods(
    modules: dict[str, ast.Module], top: str, out: dict[str, str]
) -> None:
    """Copy ``pkg.mod.Class.method`` onto ``pkg.Class.method`` when ``pkg`` re-exports Class."""
    for name, tree in modules.items():
        if name != top and not name.startswith(top + "."):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 0 and not node.module:
                continue
            base = _from_module(name, node)
            if not base:
                continue
            for alias in node.names:
                if alias.name == "*" or alias.name.startswith("_"):
                    continue
                local = alias.asname or alias.name
                exported = f"{name}.{local}"
                source = f"{base}.{alias.name}"
                prefix = source + "."
                for symbol, signature in list(out.items()):
                    if symbol == source or symbol.startswith(prefix):
                        alias_name = exported + symbol[len(source) :]
                        out.setdefault(alias_name, signature.replace(source, exported, 1))


def _from_module(owner: str, node: ast.ImportFrom) -> str:
    """Resolve ``from ... import`` target module for ``owner`` (dotted package path).

    Wheels map ``pkg/__init__.py`` to dotted ``pkg``, so a package owner is the
    package itself. ``from .client import X`` inside ``pkg`` resolves to ``pkg.client``.
    """
    if node.level:
        parts = owner.split(".")
        climb = node.level - 1
        if climb > 0:
            parts = parts[:-climb] if len(parts) >= climb else []
        suffix = (node.module or "").split(".") if node.module else []
        return ".".join([*parts, *[part for part in suffix if part]])
    return node.module or ""


class _PublicSurfaceCheck(AstCheck):
    """One-pass public class / method / assignment / import names for a module."""

    wants = (
        ast.ClassDef,
        ast.Assign,
        ast.AnnAssign,
        ast.Import,
        ast.ImportFrom,
    )

    def __init__(self, module: str) -> None:
        self._module = module
        self._rows: dict[str, str] = {}

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                return
            qual = f"{self._module}.{node.name}"
            self._rows.setdefault(qual, f"{qual}()")
            for body in node.body:
                if isinstance(body, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if body.name.startswith("_"):
                        continue
                    method = f"{qual}.{body.name}"
                    self._rows.setdefault(method, _cheap_signature(method, body))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                if local != "*" and not local.startswith("_"):
                    qual = f"{self._module}.{local}"
                    self._rows.setdefault(qual, f"{qual}()")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    qual = f"{self._module}.{target.id}"
                    self._rows.setdefault(qual, f"{qual}()")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                qual = f"{self._module}.{node.target.id}"
                self._rows.setdefault(qual, f"{qual}()")

    def finish(self) -> dict[str, str]:
        return self._rows


def _module_level_functions(module: str, tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            qual = f"{module}.{node.name}"
            out[qual] = _cheap_signature(qual, node)
    return out


def _cheap_signature(qual: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        text = f"{qual}({ast.unparse(node.args)})"
    except Exception:  # noqa: BLE001 — unparse can fail on odd nodes
        text = f"{qual}()"
    if node.returns is not None:
        try:
            text += f" -> {ast.unparse(node.returns)}"
        except Exception:  # noqa: BLE001
            pass
    return text[:_MAX_SIGNATURE_CHARS]


def _params_from_signature(signature: str) -> tuple[str, ...]:
    if "(" not in signature or ")" not in signature:
        return ()
    inner = signature[signature.index("(") + 1 : signature.rindex(")")].strip()
    if not inner:
        return ()
    names: list[str] = []
    for part in inner.split(","):
        part = part.strip()
        if not part or part == "*":
            continue
        name = part.lstrip("*").split(":", 1)[0].split("=", 1)[0].strip()
        if name and name not in {"self", "cls", "args", "kwargs"}:
            names.append(name)
    return tuple(names)


def name_breaks(
    modules: dict[str, ast.Module],
    path: str,
    source: str,
    span: tuple[int, int],
    package: str,
    version: str,
    *,
    original: str = "",
    all_imports: bool = False,
    wheel: Path | None = None,
    python: str | None = None,
) -> list[str]:
    """Findings for SDK imports and module-rooted attribute chains the rewrite uses.

    Checks:
    - ``import pkg...`` / ``from pkg... import name`` that the rewrite introduced,
      or whose local name is used inside the rewritten span — or, when
      ``all_imports`` is True (patched-file recheck), **every** package import
      in the file, including stale leftovers the rewrite left behind
    - ``pkg.sub.Name`` attribute chains inside the rewritten span

    Names that cannot be confirmed (runtime probe failed, or static lazy
    surface) are skipped — never reported as invented.

    Attribute reads on values typed as a release class are covered elsewhere
    (``attribute_breaks`` / ``missing_member``).
    """
    top = _top(package)
    if not top or not package or not version:
        return []
    has_modules = any(name == top or name.startswith(top + ".") for name in modules)
    if not has_modules and wheel is None and python is None:
        # No Python modules for this package in the wheel: cannot decide.
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    try:
        before = ast.parse(original) if original.strip() else ast.Module(body=[], type_ignores=[])
    except (SyntaxError, ValueError):
        before = ast.Module(body=[], type_ignores=[])

    old_imports = {
        _import_key(node) for node in before.body if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    used = _names_used_in_span(tree, span)
    out: list[str] = []

    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if not _targets_package(node, top):
            continue
        local_names = {(alias.asname or alias.name.split(".")[0]) for alias in node.names}
        introduced = _import_key(node) not in old_imports
        if not all_imports and not introduced and not (used & local_names):
            continue
        for text in _check_import(
            modules, node, path, package, version, wheel=wheel, python=python
        ):
            if text not in out:
                out.append(text)

    aliases = _module_aliases(tree, top, modules)
    for node in _chain_tips(tree, span):
        root, parts = _attr_chain(node)
        if root is None or root.id not in aliases:
            continue
        for text in _check_chain(
            modules,
            path,
            node,
            aliases[root.id],
            parts,
            package,
            version,
            wheel=wheel,
            python=python,
        ):
            if text not in out:
                out.append(text)
    return out


def merge_candidates(
    existing: list[dict] | None,
    grounded: list[dict[str, str]],
    *,
    limit: int = MAX_GROUND_CANDIDATES,
) -> list[dict[str, str]]:
    """Prefer impact candidates, then append grounded names up to ``limit``."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in [*(existing or []), *(grounded or [])]:
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(
            {
                "symbol": symbol,
                "signature": str(row.get("signature") or "")[:_MAX_SIGNATURE_CHARS],
            }
        )
        if len(out) >= limit:
            break
    return out


def _top(package: str) -> str:
    return package.lower().replace("-", "_")


def _import_key(node: ast.AST) -> tuple:
    if isinstance(node, ast.Import):
        return ("import", tuple((a.name, a.asname) for a in node.names))
    if isinstance(node, ast.ImportFrom):
        return (
            "from",
            node.level,
            node.module or "",
            tuple((a.name, a.asname) for a in node.names),
        )
    return ("?",)


def _targets_package(node: ast.AST, top: str) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == top or alias.name.startswith(top + ".") for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return False
        module = node.module or ""
        return module == top or module.startswith(top + ".")
    return False


def _names_used_in_span(tree: ast.Module, span: tuple[int, int]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None or not (span[0] <= lineno <= span[1]):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            root, _parts = _attr_chain(node)
            if root is not None:
                names.add(root.id)
    return names


def _module_aliases(
    tree: ast.Module, top: str, modules: dict[str, ast.Module]
) -> dict[str, str]:
    """Local name -> dotted module path for SDK module imports used as attribute roots."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == top or alias.name.startswith(top + "."):
                    out[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            module = node.module
            if module != top and not module.startswith(top + "."):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                child = f"{module}.{alias.name}"
                if _module_present(modules, child):
                    out[alias.asname or alias.name] = child
    return out


def _chain_tips(tree: ast.Module, span: tuple[int, int]) -> list[ast.Attribute]:
    """Outermost ``Attribute`` nodes in ``span`` (full chains, not intermediate links)."""
    parents = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    tips: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not (span[0] <= getattr(node, "lineno", 0) <= span[1]):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            continue
        tips.append(node)
    return tips


def _attr_chain(node: ast.AST) -> tuple[ast.Name | None, list[str]]:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    parts.reverse()
    return (cur if isinstance(cur, ast.Name) else None), parts


def _check_import(
    modules: dict[str, ast.Module],
    node: ast.Import | ast.ImportFrom,
    path: str,
    package: str,
    version: str,
    *,
    wheel: Path | None = None,
    python: str | None = None,
) -> list[str]:
    out: list[str] = []
    label = ast.unparse(node).splitlines()[0][:80]
    line = node.lineno
    if isinstance(node, ast.Import):
        for alias in node.names:
            status, missing = _module_status(
                modules, alias.name, package, wheel=wheel, python=python
            )
            if status != "missing" or missing is None:
                continue
            out.append(_module_finding(path, line, label, missing, package, version, modules))
        return out

    module = node.module or ""
    if any(alias.name == "*" for alias in node.names):
        # Star import: cannot enumerate; only require the module itself.
        status, missing = _module_status(
            modules, module, package, wheel=wheel, python=python
        )
        if status == "missing" and missing is not None:
            out.append(_module_finding(path, line, label, missing, package, version, modules))
        return out

    status, missing = _module_status(
        modules, module, package, wheel=wheel, python=python
    )
    if status == "missing" and missing is not None:
        out.append(_module_finding(path, line, label, missing, package, version, modules))
        return out
    if status == "unknown":
        return out

    for alias in node.names:
        dotted = f"{module}.{alias.name}"
        name_status, _ = resolve_name(
            dotted, package, modules, wheel=wheel, python=python
        )
        if name_status != "missing":
            continue
        candidates = _names_nearby(modules, module)
        out.append(
            f"{path}:{line} {label}: {module}.{alias.name} {DOES_NOT_EXIST} {package} {version}"
            + (f" (names in {module}: {', '.join(candidates)})" if candidates else "")
        )
    return out


def _check_chain(
    modules: dict[str, ast.Module],
    path: str,
    node: ast.Attribute,
    base: str,
    parts: list[str],
    package: str,
    version: str,
    *,
    wheel: Path | None = None,
    python: str | None = None,
) -> list[str]:
    if not parts:
        return []
    current = base
    label = ast.unparse(node)[:80]
    line = node.lineno
    for part in parts:
        child = f"{current}.{part}"
        status, _ = resolve_name(
            child, package, modules, wheel=wheel, python=python
        )
        if status == "ok":
            # Prefer continuing along submodules when present.
            if _module_present(modules, child):
                current = child
                continue
            # A value (class/function/constant): further attributes are not module paths.
            return []
        if status == "unknown":
            return []
        # missing
        if not _module_present(modules, current) and current not in modules:
            # Parent itself unknown/absent under static surface — do not invent.
            parent_status, _ = resolve_name(
                current, package, modules, wheel=wheel, python=python
            )
            if parent_status != "ok":
                return []
        nearby: list[str] = []
        for name in _child_modules(modules, current) + _names_nearby(modules, current):
            if name not in nearby:
                nearby.append(name)
            if len(nearby) >= _MAX_NEARBY:
                break
        return [
            f"{path}:{line} {label}: {child} {DOES_NOT_EXIST} {package} {version}"
            + (f" (names in {current}: {', '.join(nearby)})" if nearby else "")
        ]
    return []


def _module_status(
    modules: dict[str, ast.Module],
    dotted: str,
    package: str,
    *,
    wheel: Path | None = None,
    python: str | None = None,
) -> tuple[str, str | None]:
    """``(status, missing_prefix)`` for a module path. ``missing`` only when status is missing."""
    if not dotted:
        return "ok", None
    status, _ = resolve_name(dotted, package, modules, wheel=wheel, python=python)
    if status == "ok":
        return "ok", None
    if status == "unknown":
        return "unknown", None
    # missing: report the longest missing prefix with a known parent
    parts = dotted.split(".")
    for length in range(1, len(parts) + 1):
        candidate = ".".join(parts[:length])
        cand_status, _ = resolve_name(
            candidate, package, modules, wheel=wheel, python=python
        )
        if cand_status == "missing":
            parent = ".".join(parts[: length - 1]) if length > 1 else ""
            if length == 1 or (
                parent
                and resolve_name(parent, package, modules, wheel=wheel, python=python)[0]
                == "ok"
            ):
                return "missing", candidate
            return "unknown", None
        if cand_status == "unknown":
            return "unknown", None
    return "missing", dotted


def _module_present(modules: dict[str, ast.Module] | None, name: str) -> bool:
    if not name or modules is None:
        return False
    return name in modules or any(other.startswith(name + ".") for other in modules)


def _name_in_module_static(modules: dict[str, ast.Module], module: str, name: str) -> bool:
    if _module_binds(modules.get(module), name):
        return True
    return _module_present(modules, f"{module}.{name}")


def _name_in_module(modules: dict[str, ast.Module], module: str, name: str) -> bool:
    return _name_in_module_static(modules, module, name)


def _attr_on(
    node: object,
    name: str,
    modules: dict[str, ast.Module],
    current_module: str,
    *,
    seen: frozenset[tuple[str, str]] | None = None,
) -> object | None:
    """Resolve ``name`` on a module AST or class body; used by static resolve.

    Re-export following is cycle-safe: ``(module, name)`` pairs already visited
    return None (unresolved) instead of recursing forever. Real SDK packages
    often re-export from each other or from themselves.
    """
    key = (current_module, name)
    visited = seen or frozenset()
    if key in visited:
        return None
    visited = visited | {key}
    # Cap walk length so pathological graphs cannot blow the stack.
    if len(visited) > 64:
        return None
    if isinstance(node, ast.Module):
        for item in node.body:
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == name:
                    return item
            elif isinstance(item, ast.ImportFrom):
                for alias in item.names:
                    local = alias.asname or alias.name
                    if local != name or alias.name == "*":
                        continue
                    child = f"{current_module}.{name}"
                    if child in modules:
                        return modules[child]
                    # Follow re-export to the defining module's binding.
                    base = _from_module(current_module, item)
                    if base:
                        source = modules.get(base)
                        if source is not None:
                            found = _attr_on(
                                source, alias.name, modules, base, seen=visited
                            )
                            if found is not None:
                                return found
                    return item
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    local = alias.asname or alias.name.split(".", 1)[0]
                    if local == name:
                        child = f"{current_module}.{name}"
                        if child in modules:
                            return modules[child]
                        return item
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return item
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                if item.target.id == name:
                    return item
        child = f"{current_module}.{name}"
        if child in modules or any(m.startswith(child + ".") for m in modules):
            return modules.get(child) or ast.Module(body=[], type_ignores=[])
        return None
    if isinstance(node, ast.ClassDef):
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if item.name == name:
                    return item
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return item
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                if item.target.id == name:
                    return item
        return None
    return None



def _names_nearby(modules: dict[str, ast.Module], module: str) -> list[str]:
    tree = modules.get(module)
    if tree is None:
        return _child_modules(modules, module)[:_MAX_NEARBY]
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                local = alias.asname or alias.name
                if local != "*" and not local.startswith("_"):
                    names.append(local)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                names.append(node.target.id)
    for child in _child_modules(modules, module):
        if child not in names:
            names.append(child)
    # Stable, short list for the model prompt.
    return sorted(dict.fromkeys(names))[:_MAX_NEARBY]


def _child_modules(modules: dict[str, ast.Module], parent: str) -> list[str]:
    prefix = parent + "."
    children: set[str] = set()
    for name in modules:
        if name.startswith(prefix):
            rest = name[len(prefix) :].split(".", 1)[0]
            if rest and not rest.startswith("_"):
                children.add(rest)
    return sorted(children)


@dataclass(frozen=True)
class RemovedImport:
    """One ``from <removed module> import ...`` in a consumer file.

    ``moved`` is ``(imported name, dotted home on the target surface)``.
    ``missing`` names have no equivalent anywhere on that surface.
    ``kind_mismatches`` is ``(name, old_kind, new_kind)`` when a same-name
    candidate exists but is a different kind (exception, class, callable, or
    module); those names are also listed in ``missing`` and are not rewritten.
    ``replacement`` is empty when nothing moved: the statement stays, and the
    site needs a human. When anything moved, ``replacement`` is the new import
    block (names with a home only) and the removed module is not imported.
    """

    path: str
    line: int
    end_line: int
    module: str
    moved: tuple[tuple[str, str], ...]
    missing: tuple[str, ...]
    replacement: str
    kind_mismatches: tuple[tuple[str, str, str], ...] = ()


def _kind_label(kind: str) -> str:
    """Human label for a symbol kind. ``exc_type`` reads as ``exception``."""
    if kind == "exc_type":
        return "exception"
    return kind


class RemovedModuleImports:
    """Relocate imports whose entire module is gone from one target release.

    Homes come from the wheel's public surface (static) plus one runtime
    ``import`` + ``getattr`` of ``package.<name>`` for lazy re-exports. The
    same home cache is shared across files so every file is rewritten the
    same way. No vendor or module names are special-cased.
    """

    def __init__(
        self,
        *,
        package: str,
        version: str,
        modules: dict[str, ast.Module],
        wheel: Path | None = None,
        python: str | None = None,
        old_modules: dict[str, ast.Module] | None = None,
        old_wheel: Path | None = None,
    ) -> None:
        self.package = package
        self.version = version
        self.modules = modules
        self.wheel = wheel
        self.python = python
        self.old_modules = old_modules or {}
        self.old_wheel = old_wheel
        self.top = _top(package)
        self.surface = public_surface(modules, self.top) if modules and self.top else {}
        self._homes: dict[str, str | None] = {}
        self._kinds: dict[tuple[bool, str], str | None] = {}
        self._gone: dict[str, bool] = {}

    def plan(self, source: str, path: str) -> list[RemovedImport]:
        """Import statements in ``source`` whose module the target release removed."""
        if not self.top:
            return []
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return []
        found: list[RemovedImport] = []
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            module = node.module or ""
            if not (module == self.top or module.startswith(self.top + ".")):
                continue
            if not self._module_gone(module):
                continue
            if any(alias.name == "*" for alias in node.names):
                found.append(
                    RemovedImport(
                        path=path,
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        module=module,
                        moved=(),
                        missing=("*",),
                        replacement="",
                    )
                )
                continue
            moved: list[tuple[str, str]] = []
            missing: list[str] = []
            mismatches: list[tuple[str, str, str]] = []
            groups: dict[str, list[tuple[str, str | None]]] = {}
            order: list[str] = []
            for alias in node.names:
                home, mismatch = self._home_choice(alias.name, removed_module=module)
                if home is None:
                    missing.append(alias.name)
                    if mismatch is not None:
                        mismatches.append((alias.name, mismatch[0], mismatch[1]))
                    continue
                parent, leaf = home.rsplit(".", 1)
                moved.append((alias.name, home))
                if parent not in groups:
                    order.append(parent)
                    groups[parent] = []
                groups[parent].append((leaf, alias.asname))
            replacement = ""
            if moved:
                indent = _stmt_indent(source, node.lineno)
                lines = []
                for parent in order:
                    parts = [
                        name if not asname else f"{name} as {asname}"
                        for name, asname in groups[parent]
                    ]
                    lines.append(f"{indent}from {parent} import {', '.join(parts)}")
                replacement = "\n".join(lines) + "\n"
            found.append(
                RemovedImport(
                    path=path,
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    module=module,
                    moved=tuple(moved),
                    missing=tuple(missing),
                    replacement=replacement,
                    kind_mismatches=tuple(mismatches),
                )
            )
        return found

    def home_for(self, name: str, *, removed_module: str) -> str | None:
        """Dotted target-surface symbol for ``name``, or None if it has no home.

        A same-name candidate is accepted only when the old symbol and the
        candidate are the same kind (exception, class, callable, or module).
        A different kind, or a kind that cannot be read from either wheel,
        is not a home: the site stays needs-human.
        """
        home, _mismatch = self._home_choice(name, removed_module=removed_module)
        return home

    def _home_choice(
        self, name: str, *, removed_module: str
    ) -> tuple[str | None, tuple[str, str] | None]:
        """``(home, None)`` when accepted, else ``(None, mismatch_or_None)``.

        ``mismatch`` is ``(old_kind, new_kind)`` when a same-name candidate
        exists but the kinds differ (or either kind is unknown).
        """
        if name in self._homes:
            cached = self._homes[name]
        else:
            cached = self._find_home(name)
            self._homes[name] = cached
        if cached is None:
            return None, None
        if cached == removed_module or cached.startswith(removed_module + "."):
            return None, None
        old_kind = self._symbol_kind(f"{removed_module}.{name}", old=True)
        new_kind = self._symbol_kind(cached, old=False)
        if old_kind is None or new_kind is None or old_kind != new_kind:
            if old_kind is not None and new_kind is not None:
                return None, (old_kind, new_kind)
            return None, None
        return cached, None

    def _symbol_kind(self, dotted: str, *, old: bool) -> str | None:
        key = (old, dotted)
        if key in self._kinds:
            return self._kinds[key]
        modules = self.old_modules if old else self.modules
        wheel = self.old_wheel if old else self.wheel
        # Static surface first; runtime import + getattr when AST cannot tell.
        kind = _static_kind(dotted, self.top, modules)
        if kind is None:
            kind = _runtime_kind(dotted, self.package, wheel=wheel, python=self.python)
        self._kinds[key] = kind
        return kind

    def _find_home(self, name: str) -> str | None:
        top = self.top
        found: list[str] = []
        for symbol in self.surface:
            if symbol.rsplit(".", 1)[-1] != name:
                continue
            if symbol != top and not symbol.startswith(top + "."):
                continue
            if _static_resolve(symbol, top, self.modules) == "ok":
                found.append(symbol)
        root_attr = f"{top}.{name}"
        if root_attr not in found:
            # Lazy package re-exports are not always in the static surface.
            status, _leaf = resolve_name(
                root_attr,
                self.package,
                self.modules,
                wheel=self.wheel,
                python=self.python,
            )
            if status == "ok":
                found.append(root_attr)
        if not found:
            return None

        def rank(symbol: str) -> tuple:
            parts = symbol.split(".")
            private = sum(part.startswith("_") for part in parts)
            return (private, len(parts), symbol)

        return min(found, key=rank)

    def _module_gone(self, module: str) -> bool:
        cached = self._gone.get(module)
        if cached is not None:
            return cached
        status, missing = _module_status(
            self.modules,
            module,
            self.package,
            wheel=self.wheel,
            python=self.python,
        )
        gone = bool(
            status == "missing"
            and missing
            and (module == missing or module.startswith(missing + "."))
        )
        self._gone[module] = gone
        return gone

    def rewrite_attribute_imports(
        self, source: str, path: str
    ) -> tuple[str, list[RemovedImport]]:
        """Rewrite ``import pkg.gone`` and ``pkg.gone.Name`` from the target surface.

        A name that exists on the target becomes its real dotted home, and the
        import becomes whatever that home needs. A name with no home is left as
        the bare identifier and recorded as missing so the site stays
        needs-human. The gone module is not kept when every use was an attribute.
        """
        if not self.top:
            return source, []
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return source, []
        bindings: list[tuple[ast.Import, str, tuple[str, ...]]] = []
        for node in tree.body:
            if not isinstance(node, ast.Import):
                continue
            for alias in node.names:
                module = alias.name
                if not self._attribute_module_gone(module):
                    continue
                prefix = (alias.asname,) if alias.asname else tuple(module.split("."))
                bindings.append((node, module, prefix))
        if not bindings:
            return source, []
        prefix_module = {prefix: module for _node, module, prefix in bindings}
        symbol_values: set[int] = set()
        uses: list[tuple[ast.Attribute, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            parts = _expr_parts(node.value)
            module = prefix_module.get(parts) if parts is not None else None
            if module is None or not node.attr.isidentifier():
                continue
            uses.append((node, module))
            symbol_values.add(id(node.value))
        bare_modules: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            parts = _expr_parts(node)
            module = prefix_module.get(parts) if parts is not None else None
            if module is not None and id(node) not in symbol_values:
                bare_modules.add(module)
        if not uses and not bare_modules:
            return source, []

        moved_by_module: dict[str, list[tuple[str, str]]] = {}
        missing_by_module: dict[str, list[str]] = {}
        mismatch_by_module: dict[str, list[tuple[str, str, str]]] = {}
        parents: list[str] = []
        edits: list[tuple[int, int, str]] = []
        use_hits: dict[int, list[tuple[str, str | None, str, tuple[str, str] | None]]] = {}
        line_starts = _line_starts(source)
        for node, module in uses:
            home, mismatch = self._home_choice(node.attr, removed_module=module)
            if home is None:
                missing_by_module.setdefault(module, [])
                if node.attr not in missing_by_module[module]:
                    missing_by_module[module].append(node.attr)
                if mismatch is not None:
                    mismatch_by_module.setdefault(module, [])
                    row = (node.attr, mismatch[0], mismatch[1])
                    if row not in mismatch_by_module[module]:
                        mismatch_by_module[module].append(row)
                replacement = node.attr
                recorded: str | None = None
            else:
                parent = home.rsplit(".", 1)[0]
                if parent not in parents:
                    parents.append(parent)
                moved_by_module.setdefault(module, [])
                pair = (node.attr, home)
                if pair not in moved_by_module[module]:
                    moved_by_module[module].append(pair)
                replacement = home
                recorded = home
                mismatch = None
            start, end = _expr_span(line_starts, node)
            edits.append((start, end, replacement))
            use_hits.setdefault(node.lineno, []).append((module, recorded, node.attr, mismatch))

        by_stmt: dict[int, tuple[ast.Import, list[str]]] = {}
        for node, module, _prefix in bindings:
            slot = by_stmt.setdefault(id(node), (node, []))
            if module not in slot[1]:
                slot[1].append(module)
        existing = _imported_modules_bound(tree)
        for node, modules in by_stmt.values():
            if any(module in bare_modules for module in modules):
                continue
            for alias in node.names:
                if not alias.asname and self._attribute_module_gone(alias.name):
                    existing.discard(alias.name)
        needed = [parent for parent in parents if parent not in existing]
        hits: list[RemovedImport] = []
        placed_new_imports = False
        for node, modules in by_stmt.values():
            if any(module in bare_modules for module in modules):
                for module in modules:
                    hits.append(
                        RemovedImport(
                            path=path,
                            line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            module=module,
                            moved=tuple(moved_by_module.get(module, ())),
                            missing=tuple(missing_by_module.get(module, ())) or (module,),
                            replacement="",
                            kind_mismatches=tuple(mismatch_by_module.get(module, ())),
                        )
                    )
                continue
            start, end = _stmt_span(source, node.lineno, node.end_lineno or node.lineno)
            if not placed_new_imports:
                kept = [
                    alias.name if not alias.asname else f"{alias.name} as {alias.asname}"
                    for alias in node.names
                    if not self._attribute_module_gone(alias.name)
                ]
                indent = _stmt_indent(source, node.lineno)
                lines = [f"{indent}import {name}" for name in kept]
                lines.extend(f"{indent}import {parent}" for parent in needed)
                text = ("\n".join(lines) + "\n") if lines else ""
                edits.append((start, end, text))
                placed_new_imports = True
                replacement = text
            else:
                edits.append((start, end, ""))
                replacement = ""
            for module in modules:
                hits.append(
                    RemovedImport(
                        path=path,
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        module=module,
                        moved=tuple(moved_by_module.get(module, ())),
                        missing=tuple(missing_by_module.get(module, ())),
                        replacement=replacement if module == modules[0] else "",
                        kind_mismatches=tuple(mismatch_by_module.get(module, ())),
                    )
                )
        for line, items in sorted(use_hits.items()):
            moved = tuple((name, home) for _module, home, name, _mm in items if home)
            missing = tuple(name for _module, home, name, _mm in items if not home)
            mismatches = tuple(
                (name, mm[0], mm[1])
                for _module, home, name, mm in items
                if not home and mm is not None
            )
            module = items[0][0]
            hits.append(
                RemovedImport(
                    path=path,
                    line=line,
                    end_line=line,
                    module=module,
                    moved=moved,
                    missing=missing,
                    replacement="",
                    kind_mismatches=mismatches,
                )
            )
        if not edits:
            return source, hits
        updated = source
        for start, end, text in sorted(edits, key=lambda item: item[0], reverse=True):
            updated = updated[:start] + text + updated[end:]
        return updated, hits

    def _attribute_module_gone(self, module: str) -> bool:
        if not module or module == self.top:
            return False
        if not (module == self.top or module.startswith(self.top + ".")):
            return False
        return self._module_gone(module)

    def rewrite_calls_onto_client(
        self, source: str, path: str
    ) -> tuple[str, list[RemovedImport]]:
        """Move removed module-level calls onto a client instance when unambiguous.

        Detects names that exist on the source wheel's module surface but are
        missing from the target, while a same-named or clearly matching method
        lives on a public client class (constructor + methods). Inserts one
        client construction near the first use (or reuses an existing one),
        carrying config kwargs from a removed ``init`` call when present.

        Subclassing a removed class is not rewritten into an invented base:
        those sites stay needs-human. Same-kind mismatches are refused. When
        no unique client method is grounded on the target wheel, the file is
        left alone and the name is recorded as missing.
        """
        if not self.top or not self.old_modules:
            return source, []
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return source, []
        clients = _discover_client_classes(self.modules, self.top, self.surface)
        if not clients:
            return source, []
        bindings = _package_root_bindings(tree, self.top)
        from_imports = _package_from_imports(tree, self.top)
        if not bindings and not from_imports:
            return source, []

        sites = _collect_client_move_sites(tree, bindings, from_imports, self.top)
        if not sites:
            return source, []

        method_surface = _client_method_surface(clients, self.modules, self.top)
        if not method_surface:
            return source, []

        planned: list[_ClientMovePlan] = []
        missing: list[str] = []
        mismatches: list[tuple[str, str, str]] = []
        init_kwargs: str | None = None
        init_site: _ClientMoveSite | None = None

        for site in sites:
            old_dotted = site.old_dotted
            if site.kind == "base":
                # Subclassing a removed class: do not invent a replacement base.
                if _name_missing_on_target(
                    old_dotted, self.top, self.modules, self.wheel, self.python
                ) and _name_present_on_source(
                    old_dotted, self.top, self.old_modules, self.old_wheel, self.python
                ):
                    missing.append(site.leaf)
                    planned.append(
                        _ClientMovePlan(
                            site=site,
                            action="needs_human",
                            replacement="",
                            home="",
                        )
                    )
                continue

            if not _name_missing_on_target(
                old_dotted, self.top, self.modules, self.wheel, self.python
            ):
                continue
            if not _name_present_on_source(
                old_dotted, self.top, self.old_modules, self.old_wheel, self.python
            ):
                # Also accept when the parent class/path was present and this leaf
                # was a classmethod/attr on it (nested chain already checked).
                parent = old_dotted.rsplit(".", 1)[0] if "." in old_dotted else ""
                if not (
                    parent
                    and parent != self.top
                    and _name_present_on_source(
                        parent, self.top, self.old_modules, self.old_wheel, self.python
                    )
                ):
                    continue

            if site.leaf == "init" and site.kind == "call":
                # Module ``init`` becomes client construction args; no method match required.
                init_kwargs = site.call_args_src or ""
                init_site = site
                planned.append(
                    _ClientMovePlan(
                        site=site,
                        action="init",
                        replacement="",
                        home="",
                    )
                )
                continue

            home, mismatch = self._client_method_home(
                old_dotted, site.leaf, method_surface, clients
            )
            if home is None:
                missing.append(site.leaf)
                if mismatch is not None:
                    mismatches.append((site.leaf, mismatch[0], mismatch[1]))
                planned.append(
                    _ClientMovePlan(
                        site=site,
                        action="needs_human",
                        replacement="",
                        home="",
                    )
                )
                continue
            # Ground the suggested client path against the target wheel.
            ground_name = home.ground_symbol
            status, _ = resolve_name(
                ground_name,
                self.package,
                self.modules,
                wheel=self.wheel,
                python=self.python,
            )
            if status == "missing":
                missing.append(site.leaf)
                planned.append(
                    _ClientMovePlan(
                        site=site,
                        action="needs_human",
                        replacement="",
                        home="",
                    )
                )
                continue
            if status != "ok" and ground_name not in self.surface and ground_name not in method_surface:
                # Unknown / ungrounded — do not invent.
                missing.append(site.leaf)
                planned.append(
                    _ClientMovePlan(
                        site=site,
                        action="needs_human",
                        replacement="",
                        home="",
                    )
                )
                continue
            planned.append(
                _ClientMovePlan(
                    site=site,
                    action="method",
                    replacement=home.attr_path,
                    home=home.client_qual,
                    client_short=home.client_short,
                    client_import=home.client_import,
                )
            )

        movable = [item for item in planned if item.action in {"method", "init"}]
        if not movable and not any(item.action == "needs_human" for item in planned):
            return source, []

        hits: list[RemovedImport] = []
        if not movable:
            # Only needs-human markers; leave source unchanged.
            for item in planned:
                if item.action != "needs_human":
                    continue
                hits.append(
                    RemovedImport(
                        path=path,
                        line=item.site.line,
                        end_line=item.site.end_line,
                        module=self.top,
                        moved=(),
                        missing=(item.site.leaf,),
                        replacement="",
                        kind_mismatches=tuple(
                            row for row in mismatches if row[0] == item.site.leaf
                        ),
                    )
                )
            return source, hits

        # One client class for all moves in this file when they agree.
        client_quals = {item.home for item in movable if item.action == "method" and item.home}
        if len(client_quals) > 1:
            # Ambiguous across multiple clients — refuse rather than guess.
            for item in movable:
                hits.append(
                    RemovedImport(
                        path=path,
                        line=item.site.line,
                        end_line=item.site.end_line,
                        module=self.top,
                        moved=(),
                        missing=(item.site.leaf,),
                        replacement="",
                    )
                )
            return source, hits

        method_plan = next((item for item in movable if item.action == "method"), None)
        if method_plan is None and init_site is not None:
            # Only an init call: still construct the best client if one is unique.
            if len(clients) != 1:
                hits.append(
                    RemovedImport(
                        path=path,
                        line=init_site.line,
                        end_line=init_site.end_line,
                        module=self.top,
                        moved=(),
                        missing=("init",),
                        replacement="",
                    )
                )
                return source, hits
            only = clients[0]
            client_qual = only.qual
            client_short = only.short
            client_import = only.import_from
        else:
            client_qual = method_plan.home if method_plan else next(iter(client_quals))
            client_short = method_plan.client_short if method_plan else client_qual.rsplit(".", 1)[-1]
            client_import = (
                method_plan.client_import
                if method_plan
                else client_qual.rsplit(".", 1)[0]
            )

        updated, client_var, inserted = _apply_client_move_edits(
            source,
            tree,
            planned,
            client_short=client_short,
            client_import=client_import,
            client_qual=client_qual,
            package=self.top,
            init_kwargs=init_kwargs,
            init_site=init_site,
        )
        moved_rows: list[tuple[str, str]] = []
        for item in planned:
            if item.action == "method":
                moved_rows.append((item.site.leaf, f"{client_short}.{item.replacement}"))
            elif item.action == "init":
                moved_rows.append((item.site.leaf, f"{client_short}()"))
        for item in planned:
            if item.action == "needs_human":
                hits.append(
                    RemovedImport(
                        path=path,
                        line=item.site.line,
                        end_line=item.site.end_line,
                        module=self.top,
                        moved=(),
                        missing=(item.site.leaf,),
                        replacement="",
                        kind_mismatches=tuple(
                            row for row in mismatches if row[0] == item.site.leaf
                        ),
                    )
                )
            elif item.action in {"method", "init"}:
                hits.append(
                    RemovedImport(
                        path=path,
                        line=item.site.line,
                        end_line=item.site.end_line,
                        module=self.top,
                        moved=tuple(moved_rows),
                        missing=(),
                        replacement=(
                            f"{client_var} = {client_short}({init_kwargs or ''})"
                            if inserted and item is movable[0]
                            else ""
                        ),
                    )
                )
        return updated, hits

    def _client_method_home(
        self,
        old_dotted: str,
        leaf: str,
        method_surface: dict[str, _ClientMethodHome],
        clients: list["_ClientClass"],
    ) -> tuple["_ClientMethodHome | None", tuple[str, str] | None]:
        """Pick a unique client method home for ``old_dotted``, or refuse.

        Same-leaf matches on a single client win. Otherwise ``pick_candidates``
        must rank one clear winner that still grounds on the target surface.
        Same-kind: a non-callable old symbol may only map when the site is a
        call (factory-style); otherwise a kind mismatch is returned.
        """
        by_leaf = [
            home
            for home in method_surface.values()
            if home.attr_path.rsplit(".", 1)[-1] == leaf
        ]
        # Prefer the shortest attr path on the shortest public client qual.
        by_leaf.sort(key=lambda home: (home.attr_path.count("."), len(home.client_qual), home.attr_path))
        chosen: _ClientMethodHome | None = None
        if len({(home.client_qual, home.attr_path) for home in by_leaf}) == 1:
            chosen = by_leaf[0]
        elif by_leaf:
            # Multiple same-leaf methods: only accept if they share one client
            # and one attr path after preferring non-private modules.
            public = [home for home in by_leaf if not any(
                part.startswith("_") for part in home.client_qual.split(".")
            )]
            pool = public or by_leaf
            keys = {(home.client_qual.rsplit(".", 1)[-1], home.attr_path) for home in pool}
            if len(keys) == 1:
                chosen = pool[0]
        if chosen is None:
            # Token / leaf ranking over the full client method surface.
            signatures = {
                f"{home.client_qual}.{home.attr_path}": home.signature
                for home in method_surface.values()
            }
            rows = pick_candidates(old_dotted, signatures, limit=4)
            if not rows:
                # Try leaf-only hint.
                rows = pick_candidates(leaf, signatures, limit=4)
            if not rows:
                return None, None
            top = rows[0]["symbol"]
            # Require a clear winner: unique top, or leaf match on top only.
            if len(rows) > 1:
                top_leaf = top.rsplit(".", 1)[-1]
                second = rows[1]["symbol"]
                if top_leaf != leaf and symbol_tokens(old_dotted) & symbol_tokens(top) == (
                    symbol_tokens(old_dotted) & symbol_tokens(second)
                ):
                    return None, None
            for home in method_surface.values():
                if f"{home.client_qual}.{home.attr_path}" == top:
                    chosen = home
                    break
                # Resource-class symbols may be stored under ground_symbol.
                if home.ground_symbol == top or top.endswith("." + home.attr_path):
                    chosen = home
                    break
            if chosen is None:
                # Build a home from the ranked symbol when it is clearly a
                # client attribute path we synthesized.
                for client in clients:
                    prefix = client.qual + "."
                    if top.startswith(prefix):
                        attr = top[len(prefix) :]
                        if attr in {h.attr_path for h in method_surface.values() if h.client_qual == client.qual}:
                            chosen = method_surface.get(f"{client.qual}::{attr}")
                            break
        if chosen is None:
            return None, None

        old_kind = self._symbol_kind(old_dotted, old=True)
        # Methods are callables. A removed class used as a factory call is
        # allowed onto a same-named method; other kind mismatches are refused.
        if old_kind not in {None, "callable", "class", "value"}:
            return None, None
        if old_kind == "class":
            # Only call sites may treat a removed class as a factory method.
            # Type/base uses are handled elsewhere as needs-human.
            pass
        return chosen, None


@dataclass(frozen=True)
class _ClientClass:
    qual: str
    short: str
    import_from: str
    node: ast.ClassDef
    module: str


@dataclass(frozen=True)
class _ClientMethodHome:
    client_qual: str
    client_short: str
    client_import: str
    attr_path: str
    ground_symbol: str
    signature: str


@dataclass(frozen=True)
class _ClientMoveSite:
    line: int
    end_line: int
    old_dotted: str
    leaf: str
    kind: str  # call | attr | base
    expr_node: ast.AST
    call_node: ast.Call | None = None
    call_args_src: str | None = None
    stmt_node: ast.AST | None = None


@dataclass(frozen=True)
class _ClientMovePlan:
    site: _ClientMoveSite
    action: str  # method | init | needs_human
    replacement: str
    home: str
    client_short: str = ""
    client_import: str = ""


def _discover_client_classes(
    modules: dict[str, ast.Module],
    top: str,
    surface: dict[str, str],
) -> list[_ClientClass]:
    """Public classes with a constructor and at least one public method."""
    found: list[_ClientClass] = []
    seen: set[str] = set()
    for qual, _sig in surface.items():
        if not (qual == top or qual.startswith(top + ".")):
            continue
        if qual.count(".") < 1:
            continue
        node, module = _static_defining_node(qual, top, modules)
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name.startswith("_"):
            continue
        methods = [
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not item.name.startswith("_")
        ]
        has_init = any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__"
            for item in node.body
        )
        if not has_init or not methods:
            continue
        # Skip obvious exception types — they are not clients.
        flag = _class_is_exception(node, modules, module, set())
        if flag:
            continue
        if qual in seen:
            continue
        seen.add(qual)
        parent = qual.rsplit(".", 1)[0]
        found.append(
            _ClientClass(
                qual=qual,
                short=node.name,
                import_from=parent if parent != qual else top,
                node=node,
                module=module or parent,
            )
        )
    # Prefer root-exported / shorter quals.
    found.sort(key=lambda item: (item.qual.count("."), any(p.startswith("_") for p in item.qual.split(".")), item.qual))
    # Dedupe by short name keeping the best qual.
    best: dict[str, _ClientClass] = {}
    for item in found:
        best.setdefault(item.short, item)
    return list(best.values())


def _client_method_surface(
    clients: list[_ClientClass],
    modules: dict[str, ast.Module],
    top: str,
) -> dict[str, _ClientMethodHome]:
    """``client_qual::attr.path`` → home for methods reachable on a client."""
    out: dict[str, _ClientMethodHome] = {}
    for client in clients:
        for attr_path, ground, signature in _walk_client_methods(
            client.node, client.qual, client.module, modules, top, prefix=""
        ):
            key = f"{client.qual}::{attr_path}"
            out.setdefault(
                key,
                _ClientMethodHome(
                    client_qual=client.qual,
                    client_short=client.short,
                    client_import=client.import_from,
                    attr_path=attr_path,
                    ground_symbol=ground,
                    signature=signature,
                ),
            )
    return out


def _walk_client_methods(
    node: ast.ClassDef,
    class_qual: str,
    module: str,
    modules: dict[str, ast.Module],
    top: str,
    *,
    prefix: str,
    depth: int = 0,
) -> list[tuple[str, str, str]]:
    """Attribute paths and ground symbols for methods on ``node`` and nested resources."""
    if depth > 4:
        return []
    rows: list[tuple[str, str, str]] = []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_") and item.name != "__init__":
                continue
            if item.name == "__init__":
                continue
            attr = f"{prefix}{item.name}" if prefix else item.name
            qual = f"{class_qual}.{item.name}" if not prefix else f"{class_qual}.{attr}"
            # Property / cached_property: treat as attribute, walk return type.
            if _is_attr_property(item):
                target = _annotation_class(item.returns, module, modules, top)
                if target is not None:
                    target_node, target_mod, target_qual = target
                    rows.extend(
                        _walk_client_methods(
                            target_node,
                            target_qual,
                            target_mod,
                            modules,
                            top,
                            prefix=attr + ".",
                            depth=depth + 1,
                        )
                    )
                continue
            rows.append((attr, qual if not prefix else _nested_ground(class_qual, attr, item, module, modules), _cheap_signature(qual, item)))
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            name = item.target.id
            if name.startswith("_"):
                continue
            target = _annotation_class(item.annotation, module, modules, top)
            if target is None:
                continue
            target_node, target_mod, target_qual = target
            attr = f"{prefix}{name}" if prefix else name
            rows.extend(
                _walk_client_methods(
                    target_node,
                    target_qual,
                    target_mod,
                    modules,
                    top,
                    prefix=attr + ".",
                    depth=depth + 1,
                )
            )
    # Also follow ``self.attr = Resource(self)`` assigns inside ``__init__``.
    for item in node.body:
        if not (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__"):
            continue
        for stmt in item.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                continue
            name = target.attr
            if name.startswith("_"):
                continue
            callee = stmt.value
            if not isinstance(callee, ast.Call):
                continue
            resolved = _call_class(callee.func, module, modules, top)
            if resolved is None:
                continue
            target_node, target_mod, target_qual = resolved
            attr = f"{prefix}{name}" if prefix else name
            rows.extend(
                _walk_client_methods(
                    target_node,
                    target_qual,
                    target_mod,
                    modules,
                    top,
                    prefix=attr + ".",
                    depth=depth + 1,
                )
            )
    return rows


def _nested_ground(
    class_qual: str,
    attr_path: str,
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    modules: dict[str, ast.Module],
) -> str:
    """Best-effort dotted symbol for grounding a nested client method."""
    return f"{class_qual}.{attr_path}"


def _is_attr_property(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id in {"property", "cached_property"}:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr in {"property", "cached_property"}:
            return True
    return False


def _annotation_class(
    annotation: ast.expr | None,
    module: str,
    modules: dict[str, ast.Module],
    top: str,
) -> tuple[ast.ClassDef, str, str] | None:
    if annotation is None:
        return None
    return _resolve_class_expr(annotation, module, modules, top)


def _call_class(
    func: ast.expr,
    module: str,
    modules: dict[str, ast.Module],
    top: str,
) -> tuple[ast.ClassDef, str, str] | None:
    return _resolve_class_expr(func, module, modules, top)


def _resolve_class_expr(
    expr: ast.expr,
    module: str,
    modules: dict[str, ast.Module],
    top: str,
) -> tuple[ast.ClassDef, str, str] | None:
    """Resolve an annotation / callee expression to a ClassDef in the wheel."""
    parts = _expr_parts(expr)
    if parts is None:
        # Subscript like list[X] — ignore.
        if isinstance(expr, ast.Subscript):
            return _resolve_class_expr(expr.value, module, modules, top)
        return None
    # Relative to current module: resources.Chat, Chat, package.Chat
    candidates: list[str] = []
    dotted = ".".join(parts)
    candidates.append(dotted)
    if not dotted.startswith(top + ".") and module:
        candidates.append(f"{module}.{dotted}")
        # Climb package parents for ``resources.Chat`` from ``pkg._client``.
        mod_parts = module.split(".")
        for i in range(len(mod_parts), 0, -1):
            candidates.append(".".join([*mod_parts[:i], *parts]))
    if len(parts) == 1:
        # Same-module name or imported alias.
        tree = modules.get(module)
        if tree is not None:
            found = _attr_on(tree, parts[0], modules, module)
            if isinstance(found, ast.ClassDef):
                return found, _owner_module(found, modules) or module, f"{module}.{parts[0]}"
    for cand in candidates:
        node, owner = _static_defining_node(cand, top, modules)
        if isinstance(node, ast.ClassDef):
            return node, owner or module, cand
        # Module path then leaf class.
        if "." in cand:
            parent, leaf = cand.rsplit(".", 1)
            tree = modules.get(parent)
            if tree is not None:
                found = _attr_on(tree, leaf, modules, parent)
                if isinstance(found, ast.ClassDef):
                    return found, parent, f"{parent}.{leaf}"
    return None


def _package_root_bindings(tree: ast.AST, top: str) -> dict[str, str]:
    """Local name → package top for ``import pkg`` / ``import pkg as alias``."""
    out: dict[str, str] = {}
    for node in getattr(tree, "body", ()):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name == top:
                out[alias.asname or top] = top
    return out


def _package_from_imports(tree: ast.AST, top: str) -> dict[str, str]:
    """Local name → ``top.name`` for ``from top import name`` bindings."""
    out: dict[str, str] = {}
    for node in getattr(tree, "body", ()):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if node.module != top:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            out[local] = f"{top}.{alias.name}"
    return out


def _collect_client_move_sites(
    tree: ast.Module,
    bindings: dict[str, str],
    from_imports: dict[str, str],
    top: str,
) -> list[_ClientMoveSite]:
    """Call / attr / base sites that reference removed package-level names."""
    sites: list[_ClientMoveSite] = []
    # Map call func id → Call for detecting calls vs bare attributes.
    call_funcs: dict[int, ast.Call] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_funcs[id(node.func)] = node

    base_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_ids.add(id(base))

    def args_src(call: ast.Call) -> str:
        try:
            text = ast.unparse(call)
            return text[text.index("(") + 1 : text.rindex(")")]
        except Exception:  # noqa: BLE001
            return ""

    # ``pkg.name`` / ``pkg.Class.method`` via import pkg
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts = _expr_parts(node)
        if parts is None or len(parts) < 2:
            continue
        root = parts[0]
        if root in bindings:
            chain = parts[1:]
            old_dotted = ".".join([top, *chain])
            leaf = chain[-1]
            kind = "attr"
            call = call_funcs.get(id(node))
            if id(node) in base_ids:
                kind = "base"
                # For bases, the removed name is the class (first chain segment
                # when nested, or the leaf).
                if len(chain) >= 1:
                    old_dotted = f"{top}.{chain[0]}"
                    leaf = chain[0]
            elif call is not None:
                kind = "call"
            sites.append(
                _ClientMoveSite(
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", None) or node.lineno,
                    old_dotted=old_dotted,
                    leaf=leaf,
                    kind=kind,
                    expr_node=node,
                    call_node=call,
                    call_args_src=args_src(call) if call is not None else None,
                    stmt_node=None,
                )
            )
        elif root in from_imports:
            # ``Name.method`` where Name was ``from pkg import Name``.
            imported = from_imports[root]
            chain = parts[1:]
            old_dotted = ".".join([imported, *chain]) if chain else imported
            leaf = chain[-1] if chain else root
            kind = "attr"
            call = call_funcs.get(id(node))
            if id(node) in base_ids:
                kind = "base"
                old_dotted = imported
                leaf = imported.rsplit(".", 1)[-1]
            elif call is not None:
                kind = "call"
            # Only interested when this is a method on an imported class, or
            # the imported name itself used as a call that may move.
            sites.append(
                _ClientMoveSite(
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", None) or node.lineno,
                    old_dotted=old_dotted,
                    leaf=leaf,
                    kind=kind,
                    expr_node=node,
                    call_node=call,
                    call_args_src=args_src(call) if call is not None else None,
                )
            )

    # Bare ``Name(...)`` where Name came from ``from pkg import Name``.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in from_imports:
            continue
        imported = from_imports[node.func.id]
        if id(node.func) in base_ids:
            continue
        sites.append(
            _ClientMoveSite(
                line=node.lineno,
                end_line=getattr(node, "end_lineno", None) or node.lineno,
                old_dotted=imported,
                leaf=imported.rsplit(".", 1)[-1],
                kind="call",
                expr_node=node.func,
                call_node=node,
                call_args_src=args_src(node),
            )
        )

    # ``class X(Name)`` where Name came from from-import.
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in from_imports:
                imported = from_imports[base.id]
                sites.append(
                    _ClientMoveSite(
                        line=base.lineno,
                        end_line=getattr(base, "end_lineno", None) or base.lineno,
                        old_dotted=imported,
                        leaf=imported.rsplit(".", 1)[-1],
                        kind="base",
                        expr_node=base,
                    )
                )
    return sites


def _name_missing_on_target(
    dotted: str,
    top: str,
    modules: dict[str, ast.Module],
    wheel: Path | None,
    python: str | None,
) -> bool:
    status = _static_resolve(dotted, top, modules)
    if status == "ok":
        return False
    if status == "missing":
        # Confirm with runtime when possible (lazy exports).
        if wheel is not None:
            runtime, _ = resolve_name(dotted, top, modules, wheel=wheel, python=python)
            if runtime == "ok":
                return False
            if runtime == "missing":
                return True
            # unknown → treat static missing as missing for mechanical moves
        return True
    if status == "unknown" and wheel is not None:
        runtime, _ = resolve_name(dotted, top, modules, wheel=wheel, python=python)
        return runtime == "missing"
    return False


def _name_present_on_source(
    dotted: str,
    top: str,
    modules: dict[str, ast.Module],
    wheel: Path | None,
    python: str | None,
) -> bool:
    status = _static_resolve(dotted, top, modules)
    if status == "ok":
        return True
    if wheel is not None:
        runtime, _ = resolve_name(dotted, top, modules, wheel=wheel, python=python)
        return runtime == "ok"
    return False


def _apply_client_move_edits(
    source: str,
    tree: ast.Module,
    planned: list[_ClientMovePlan],
    *,
    client_short: str,
    client_import: str,
    client_qual: str,
    package: str,
    init_kwargs: str | None,
    init_site: _ClientMoveSite | None,
) -> tuple[str, str, bool]:
    """Apply call rewrites and ensure one client construction. Returns var name."""
    existing = _find_existing_client_var(tree, client_short)
    client_var = existing or _pick_client_var_name(tree, client_short)
    inserted = existing is None

    edits: list[tuple[int, int, str]] = []
    line_starts = _line_starts(source)
    init_replaced = False

    for item in planned:
        if item.action == "needs_human":
            continue
        if item.action == "init":
            stmt = _stmt_containing(tree, item.site.line)
            if stmt is None:
                continue
            start, end = _stmt_span(
                source, stmt.lineno, getattr(stmt, "end_lineno", None) or stmt.lineno
            )
            if inserted and not init_replaced:
                # Replace the init call with the client construction in one edit
                # so insert+delete offsets cannot overlap.
                indent = _stmt_indent(source, stmt.lineno)
                kwargs = init_kwargs or ""
                edits.append(
                    (start, end, f"{indent}{client_var} = {client_short}({kwargs})\n")
                )
                init_replaced = True
            else:
                # Client already exists (or construction inserted elsewhere): drop init.
                edits.append((start, end, ""))
            continue
        if item.action == "method":
            node = item.site.expr_node
            start, end = _expr_span(line_starts, node)
            edits.append((start, end, f"{client_var}.{item.replacement}"))

    # Import the client class when needed.
    import_line = _ensure_client_import_edit(
        source, tree, client_short=client_short, client_import=client_import, package=package
    )
    if import_line is not None:
        edits.append(import_line)

    # Insert construction near first movable use when no existing client and no init.
    if inserted and not init_replaced:
        first_line = min(
            (item.site.line for item in planned if item.action == "method"),
            default=1,
        )
        indent = _stmt_indent(source, first_line)
        kwargs = init_kwargs or ""
        construction = f"{indent}{client_var} = {client_short}({kwargs})\n"
        starts = _line_starts(source)
        pos = starts[first_line - 1] if 1 <= first_line <= len(starts) else 0
        edits.append((pos, pos, construction))

    if not edits:
        return source, client_var, False
    updated = source
    for start, end, text in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        updated = updated[:start] + text + updated[end:]
    try:
        ast.parse(updated)
    except (SyntaxError, ValueError):
        return source, client_var, False
    return updated, client_var, inserted


def _find_existing_client_var(tree: ast.Module, client_short: str) -> str | None:
    """Return the local name of an existing ``Client(...)`` construction, if any."""
    for node in getattr(tree, "body", ()):
        found = _client_var_in_assign(node, client_short)
        if found:
            return found
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for stmt in node.body:
                found = _client_var_in_assign(stmt, client_short)
                if found:
                    return found
    return None


def _client_var_in_assign(node: ast.AST, client_short: str) -> str | None:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    if not isinstance(target, ast.Name):
        return None
    value = node.value
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    if isinstance(func, ast.Name) and func.id == client_short:
        return target.id
    if isinstance(func, ast.Attribute) and func.attr == client_short:
        return target.id
    return None


def _pick_client_var_name(tree: ast.Module, client_short: str) -> str:
    bound = set(_bound_names(tree))
    if "client" not in bound:
        return "client"
    snake = _camel_to_snake(client_short) + "_client"
    if snake not in bound:
        return snake
    base = snake
    index = 2
    while f"{base}{index}" in bound:
        index += 1
    return f"{base}{index}"


def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for index, ch in enumerate(name):
        if ch.isupper() and index:
            out.append("_")
        out.append(ch.lower())
    return "".join(out) or "client"


def _bound_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def _ensure_client_import_edit(
    source: str,
    tree: ast.Module,
    *,
    client_short: str,
    client_import: str,
    package: str,
) -> tuple[int, int, str] | None:
    """Insert ``from <module> import Client`` when the class is not already bound."""
    if client_short in _bound_names(tree):
        # May already be imported or defined.
        for node in getattr(tree, "body", ()):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if (alias.asname or alias.name) == client_short:
                        return None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == client_import or alias.name.endswith("." + client_short):
                        return None
    # Prefer ``from package import Client`` when client_import is the package
    # or a private module that re-exports at package root.
    module = package if client_import == package or client_import.startswith(package + "._") else client_import
    # If the defining module is private, import from package root when the
    # short name is on the package surface.
    if "._" in module or module.split(".")[-1].startswith("_"):
        module = package
    line = f"from {module} import {client_short}\n"
    # Insert after the last existing import, or at file start.
    last_import_end = 0
    for node in getattr(tree, "body", ()):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_end = getattr(node, "end_lineno", None) or node.lineno
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            # docstring
            continue
        else:
            break
    if last_import_end:
        starts = _line_starts(source)
        if last_import_end < len(starts):
            pos = starts[last_import_end]
        else:
            pos = len(source)
        return (pos, pos, line)
    return (0, 0, line)


def _stmt_containing(tree: ast.Module, line: int) -> ast.AST | None:
    for node in getattr(tree, "body", ()):
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None) or start
        if start is not None and end is not None and start <= line <= end:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for child in node.body:
                    c_start = getattr(child, "lineno", None)
                    c_end = getattr(child, "end_lineno", None) or c_start
                    if c_start is not None and c_end is not None and c_start <= line <= c_end:
                        return child
            return node
    return None


def relocate_removed_imports(
    source: str,
    path: str,
    maps: list[RemovedModuleImports],
) -> tuple[str, list[RemovedImport]]:
    """Rewrite ``source`` from every map. One plan against the original text.

    ``from <gone> import Name`` is rewritten first. A plain ``import <gone>``
    plus dotted uses (``gone.Name``) is rewritten from the same homes.
    Module-level calls whose replacement is a method on a client class are
    rewritten last (one client construction, reused when already present).
    """
    hits: list[RemovedImport] = []
    for item in maps:
        hits.extend(item.plan(source, path))
    updated = source
    for hit in sorted(hits, key=lambda found: found.line, reverse=True):
        if not hit.replacement:
            continue
        updated = _replace_line_span(updated, hit.line, hit.end_line, hit.replacement)
    for item in maps:
        updated, attr_hits = item.rewrite_attribute_imports(updated, path)
        hits.extend(attr_hits)
    for item in maps:
        # Client relocation walks wheel ASTs (including re-export graphs). Any
        # unexpected failure here must not crash apply: fall through with no
        # mechanical relocation so the normal model path can run.
        try:
            updated, client_hits = item.rewrite_calls_onto_client(updated, path)
        except Exception:  # noqa: BLE001 — narrow: client relocate only
            client_hits = []
        hits.extend(client_hits)
    return updated, hits


def _stmt_indent(source: str, line: int) -> str:
    lines = source.splitlines()
    if line < 1 or line > len(lines):
        return ""
    text = lines[line - 1]
    return text[: len(text) - len(text.lstrip())]


def _expr_parts(node: ast.AST) -> tuple[str, ...] | None:
    """Dotted name parts of a ``Name`` or ``Attribute`` chain, or None."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    parts.reverse()
    return tuple(parts)


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _expr_span(starts: list[int], node: ast.AST) -> tuple[int, int]:
    lineno = getattr(node, "lineno", 1)
    end_lineno = getattr(node, "end_lineno", lineno)
    col = getattr(node, "col_offset", 0)
    end_col = getattr(node, "end_col_offset", col)
    return starts[lineno - 1] + col, starts[end_lineno - 1] + end_col


def _stmt_span(source: str, start_line: int, end_line: int) -> tuple[int, int]:
    lines = source.splitlines(keepends=True)
    start = sum(len(lines[i]) for i in range(max(start_line - 1, 0)))
    end = sum(len(lines[i]) for i in range(min(end_line, len(lines))))
    return start, end


def _imported_modules_bound(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in getattr(tree, "body", ()):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if not alias.asname:
                found.add(alias.name)
    return found


def _replace_line_span(source: str, start: int, end: int, replacement: str) -> str:
    lines = source.splitlines(keepends=True)
    prefix = "".join(lines[: start - 1])
    suffix = "".join(lines[end:])
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    return prefix + replacement + suffix


def _module_finding(
    path: str,
    line: int,
    label: str,
    missing: str,
    package: str,
    version: str,
    modules: dict[str, ast.Module],
) -> str:
    parent = missing.rsplit(".", 1)[0] if "." in missing else ""
    nearby = (
        _child_modules(modules, parent)
        if parent
        else _child_modules(modules, missing.split(".", 1)[0])
    )
    if parent:
        nearby = list(dict.fromkeys(nearby + _names_nearby(modules, parent)))[:_MAX_NEARBY]
    where = parent or missing.split(".", 1)[0]
    return (
        f"{path}:{line} {label}: {missing} {DOES_NOT_EXIST} {package} {version}"
        + (f" (names in {where}: {', '.join(nearby)})" if nearby else "")
    )
