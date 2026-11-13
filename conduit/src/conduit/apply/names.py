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
from conduit.impact.candidates import pick_candidates
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
) -> object | None:
    """Resolve ``name`` on a module AST or class body; used by static resolve."""
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
                            found = _attr_on(source, alias.name, modules, base)
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
            groups: dict[str, list[tuple[str, str | None]]] = {}
            order: list[str] = []
            for alias in node.names:
                home = self.home_for(alias.name, removed_module=module)
                if home is None:
                    missing.append(alias.name)
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
        if name in self._homes:
            cached = self._homes[name]
        else:
            cached = self._find_home(name)
            self._homes[name] = cached
        if cached is None:
            return None
        if cached == removed_module or cached.startswith(removed_module + "."):
            return None
        old_kind = self._symbol_kind(f"{removed_module}.{name}", old=True)
        new_kind = self._symbol_kind(cached, old=False)
        if old_kind is None or new_kind is None or old_kind != new_kind:
            return None
        return cached

    def _symbol_kind(self, dotted: str, *, old: bool) -> str | None:
        key = (old, dotted)
        if key in self._kinds:
            return self._kinds[key]
        modules = self.old_modules if old else self.modules
        wheel = self.old_wheel if old else self.wheel
        kind = _runtime_kind(dotted, self.package, wheel=wheel, python=self.python)
        if kind is None:
            kind = _static_kind(dotted, self.top, modules)
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
        parents: list[str] = []
        edits: list[tuple[int, int, str]] = []
        use_hits: dict[int, list[tuple[str, str | None, str]]] = {}
        line_starts = _line_starts(source)
        for node, module in uses:
            home = self.home_for(node.attr, removed_module=module)
            if home is None:
                missing_by_module.setdefault(module, [])
                if node.attr not in missing_by_module[module]:
                    missing_by_module[module].append(node.attr)
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
            start, end = _expr_span(line_starts, node)
            edits.append((start, end, replacement))
            use_hits.setdefault(node.lineno, []).append((module, recorded, node.attr))

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
                    )
                )
        for line, items in sorted(use_hits.items()):
            moved = tuple((name, home) for _module, home, name in items if home)
            missing = tuple(name for _module, home, name in items if not home)
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


def relocate_removed_imports(
    source: str,
    path: str,
    maps: list[RemovedModuleImports],
) -> tuple[str, list[RemovedImport]]:
    """Rewrite ``source`` from every map. One plan against the original text.

    ``from <gone> import Name`` is rewritten first. A plain ``import <gone>``
    plus dotted uses (``gone.Name``) is rewritten from the same homes.
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
