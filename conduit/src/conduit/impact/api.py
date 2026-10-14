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
    before = load_api(old)
    after = load_api(new)
    changes: list[ApiChange] = []
    for name in sorted(before):
        sym = before[name]
        other = after.get(name)
        if other is None:
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
            continue
        if other.deprecated and not sym.deprecated:
            changes.append(
                ApiChange(
                    kind="deprecated",
                    symbol=name,
                    old=name,
                    new=name,
                    detail=f"{_short(name)} deprecated",
                    confidence="low",
                    source="wheel",
                )
            )
        changes.extend(_signature(sym, other))
    return changes


def load_api(path: Path) -> dict[str, Symbol]:
    root = _materialize(path)
    found: dict[str, Symbol] = {}
    for py in sorted(root.rglob("*.py")):
        if not py.is_file() or _skipped(py, root):
            continue
        module = _module_name(py, root)
        if not module or module.split(".")[-1].startswith("_") and not py.name == "__init__.py":
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
