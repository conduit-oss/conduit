"""Repo functions that already build a value of a type a new-release callable requires.

Found from the repo's own code only: a function whose return annotation names
the type, or a function whose result the repo already passes as that keyword
to a callable of the same name. Nothing here knows any library's names.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "site-packages", "build", "dist", "__pycache__"}
_MAX_FILES = 4000
_MAX_HELPERS = 3
_ITERABLE_RETURNS = {"Iterable", "Sequence", "Collection", "Iterator", "List", "list", "Tuple", "tuple", "MutableSequence"}


def _sources(root: Path):
    count = 0
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if any(part in {"tests", "test"} for part in rel.parts[:-1]) or rel.name.startswith("test_"):
            continue
        count += 1
        if count > _MAX_FILES:
            return
        try:
            yield rel.as_posix(), ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue


def _type_names(annotation: ast.AST | None) -> tuple[set[str], bool]:
    """Names in a return annotation, and whether its outer type is iterable."""
    if annotation is None:
        return set(), False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value.strip(), mode="eval").body
        except SyntaxError:
            return set(), False
    names = {sub.id for sub in ast.walk(annotation) if isinstance(sub, ast.Name)}
    names |= {sub.attr for sub in ast.walk(annotation) if isinstance(sub, ast.Attribute)}
    for sub in ast.walk(annotation):  # forward references: List["Name"]
        if sub is not annotation and isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            names |= _type_names(sub)[0]
    outer = annotation
    while isinstance(outer, ast.Subscript) and _last(outer.value) in {"Optional", "Annotated"}:
        outer = outer.slice.elts[0] if isinstance(outer.slice, ast.Tuple) else outer.slice
    iterable = isinstance(outer, ast.Subscript) and _last(outer.value) in _ITERABLE_RETURNS
    return names, iterable


def _last(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", "")


def _definitions(tree: ast.Module):
    """``(qualname, def node, owner class or None)`` for module functions and methods."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node, None
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{child.name}", child, node


def _called_name(expr: ast.AST) -> str:
    if isinstance(expr, ast.Call):
        return _last(expr.func)
    return ""


def _passed_as(tree: ast.Module, keyword: str, method: str) -> set[str]:
    """Names of functions whose result this module passes as ``keyword=`` to a ``.method(...)`` call."""
    out: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigned: dict[str, str] = {}
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = _called_name(node.value)
                if name:
                    assigned[node.targets[0].id] = name
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and _last(node.func) == method):
                continue
            for kw in node.keywords:
                if kw.arg != keyword:
                    continue
                name = _called_name(kw.value) or (assigned.get(kw.value.id, "") if isinstance(kw.value, ast.Name) else "")
                if name:
                    out.add(name)
    return out


def _is_static(node: ast.AST) -> bool:
    return any(_last(deco) in {"staticmethod", "classmethod"} for deco in getattr(node, "decorator_list", []))


def _local_names(trees: dict[str, ast.Module], exclude: tuple[str, int, int] | None) -> dict[str, str]:
    """Functions the code being fixed reaches by bare name or ``self.``/``cls.``:
    its module's functions and its own class's methods, as ``name -> "path:line Qual -> ret"``."""
    if not exclude or exclude[0] not in trees:
        return {}
    path, start, _end = exclude
    out: dict[str, str] = {}
    for qualname, node, owner in _definitions(trees[path]):
        mine = owner is None or (owner.lineno <= start <= (owner.end_lineno or owner.lineno))
        if not mine:
            continue
        returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        out.setdefault(node.name, f"{qualname}{returns}")
        out[f"@{node.name}"] = f"{path}:{node.lineno}"
    return out


def repo_producers(
    root: Path,
    requirements: list[dict],
    method: str,
    exclude: tuple[str, int, int] | None = None,
) -> list[dict]:
    """Up to a few repo functions that produce what ``requirements`` ask for.

    ``requirements`` come from ``typecheck.structured_requirements``; ``method``
    is the new callable's own name; ``exclude`` is ``(path, start, end)`` of the
    function being rewritten."""
    if not requirements:
        return []
    trees = list(_sources(root))
    local = _local_names(dict(trees), exclude)

    def qualname_at(path: str, node: ast.AST) -> str:
        return f"{path}:{node.lineno}"
    scored: list[tuple[int, str, int, dict]] = []
    for req in requirements:
        wanted = set(req["names"])
        used = set()
        for _path, tree in trees:
            used |= _passed_as(tree, req["param"], method)
        for path, tree in trees:
            module = path[:-3].replace("/", ".").removesuffix(".__init__")
            for qualname, node, owner in _definitions(tree):
                if exclude and path == exclude[0] and exclude[1] <= node.lineno <= exclude[2]:
                    continue
                names, iterable = _type_names(node.returns)
                by_type = bool(names & wanted) and iterable == bool(req["iterable"])
                # Same-name functions elsewhere may return something else: an
                # annotated return has to match the type for the use to count.
                by_use = node.name in used and (node.returns is None or by_type)
                if not (by_type or by_use):
                    continue
                score = (2 if by_type else 0) + (1 if by_use else 0)
                signature = f"{qualname}({ast.unparse(node.args)})"
                if node.returns is not None:
                    signature += f" -> {ast.unparse(node.returns)}"
                top = qualname.split(".")[0]
                entry = {
                    "for": f"{req['param']}: {req['annotation']}",
                    "helper": f"{path}:{node.lineno} {signature}"[:300],
                    "import": f"from {module} import {top}",
                }
                if owner is None:
                    entry["call"] = f"{node.name}(...)"
                elif _is_static(node):
                    entry["call"] = f"{owner.name}.{node.name}(...)"
                else:
                    entry["note"] = f"instance method of {owner.name}"
                clash = local.get(node.name)
                if clash and local.get(f"@{node.name}") != qualname_at(path, node):
                    entry["caution"] = (
                        f"the code being fixed has its own {clash}, which is a different function and does not "
                        f"produce this type; call {entry.get('call', qualname)} instead"
                    )
                scored.append((-score, path, node.lineno, entry))
    scored.sort(key=lambda item: item[:3])
    out: list[dict] = []
    for *_rank, entry in scored:
        if entry not in out:
            out.append(entry)
        if len(out) >= _MAX_HELPERS:
            break
    return out
