"""Reject rewrites that import or reference names the new release does not define.

Reads the new wheel's own modules (same source as signatures / typecheck). No
package-specific tables: a name is accepted only when that module binds it or
exposes it as a submodule. Anything the wheel's Python surface cannot show is
left unknown and not flagged (C-extension-only modules, dynamic ``__getattr__``,
runtime-built import strings).
"""

from __future__ import annotations

import ast

from conduit.apply.typecheck import _module_binds

_MAX_CANDIDATES = 12
#: Distinctive phrase so the apply engine can hand off after one name retry.
DOES_NOT_EXIST = "does not exist in"


def name_breaks(
    modules: dict[str, ast.Module],
    path: str,
    source: str,
    span: tuple[int, int],
    package: str,
    version: str,
    *,
    original: str = "",
) -> list[str]:
    """Findings for SDK imports and module-rooted attribute chains the rewrite uses.

    Checks:
    - ``import pkg...`` / ``from pkg... import name`` that the rewrite introduced,
      or whose local name is used inside the rewritten span
    - ``pkg.sub.Name`` attribute chains inside the rewritten span

    Attribute reads on values typed as a release class are covered elsewhere
    (``attribute_breaks`` / ``missing_member``).
    """
    top = _top(package)
    if not top or not package or not version:
        return []
    if not any(name == top or name.startswith(top + ".") for name in modules):
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

    old_imports = {_import_key(node) for node in before.body if isinstance(node, (ast.Import, ast.ImportFrom))}
    used = _names_used_in_span(tree, span)
    out: list[str] = []

    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if not _targets_package(node, top):
            continue
        local_names = {(alias.asname or alias.name.split(".")[0]) for alias in node.names}
        introduced = _import_key(node) not in old_imports
        if not introduced and not (used & local_names):
            continue
        for text in _check_import(modules, node, path, package, version):
            if text not in out:
                out.append(text)

    aliases = _module_aliases(tree, top, modules)
    for node in _chain_tips(tree, span):
        root, parts = _attr_chain(node)
        if root is None or root.id not in aliases:
            continue
        for text in _check_chain(modules, path, node, aliases[root.id], parts, package, version):
            if text not in out:
                out.append(text)
    return out


def _top(package: str) -> str:
    return package.lower().replace("-", "_")


def _import_key(node: ast.AST) -> tuple:
    if isinstance(node, ast.Import):
        return ("import", tuple((a.name, a.asname) for a in node.names))
    if isinstance(node, ast.ImportFrom):
        return ("from", node.level, node.module or "", tuple((a.name, a.asname) for a in node.names))
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


def _module_aliases(tree: ast.Module, top: str, modules: dict[str, ast.Module]) -> dict[str, str]:
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
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
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
) -> list[str]:
    out: list[str] = []
    label = ast.unparse(node).splitlines()[0][:80]
    line = node.lineno
    if isinstance(node, ast.Import):
        for alias in node.names:
            missing = _missing_module(modules, alias.name)
            if missing is None:
                continue
            out.append(_module_finding(path, line, label, missing, package, version, modules))
        return out

    module = node.module or ""
    if any(alias.name == "*" for alias in node.names):
        # Star import: cannot enumerate; only require the module itself.
        missing = _missing_module(modules, module)
        if missing is not None:
            out.append(_module_finding(path, line, label, missing, package, version, modules))
        return out

    missing = _missing_module(modules, module)
    if missing is not None:
        out.append(_module_finding(path, line, label, missing, package, version, modules))
        return out

    for alias in node.names:
        if _name_in_module(modules, module, alias.name):
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
) -> list[str]:
    if not parts:
        return []
    current = base
    label = ast.unparse(node)[:80]
    line = node.lineno
    for index, part in enumerate(parts):
        # Prefer a submodule when both a binding and a submodule exist.
        child = f"{current}.{part}"
        if _module_present(modules, child):
            current = child
            continue
        if _name_in_module(modules, current, part):
            # A value (class/function/constant): further attributes are not module paths.
            # Typed attribute checks cover fields on release classes.
            if index < len(parts) - 1:
                return []
            return []
        # First missing segment: report it.
        if not _module_present(modules, current) and not modules.get(current):
            return []
        target = child
        if _module_present(modules, current) or current in modules:
            nearby: list[str] = []
            for name in _child_modules(modules, current) + _names_nearby(modules, current):
                if name not in nearby:
                    nearby.append(name)
                if len(nearby) >= _MAX_CANDIDATES:
                    break
            return [
                f"{path}:{line} {label}: {target} {DOES_NOT_EXIST} {package} {version}"
                + (f" (names in {current}: {', '.join(nearby)})" if nearby else "")
            ]
        return []
    return []


def _missing_module(modules: dict[str, ast.Module], dotted: str) -> str | None:
    """Longest missing prefix of ``dotted``, or None when the module is present."""
    if not dotted:
        return None
    if _module_present(modules, dotted):
        return None
    parts = dotted.split(".")
    for length in range(1, len(parts) + 1):
        candidate = ".".join(parts[:length])
        if not _module_present(modules, candidate):
            # Only flag when a parent prefix is known in this wheel.
            parent = ".".join(parts[: length - 1]) if length > 1 else ""
            if length == 1 or _module_present(modules, parent):
                return candidate
            return None
    return dotted


def _module_present(modules: dict[str, ast.Module] | None, name: str) -> bool:
    if not name or modules is None:
        return False
    return name in modules or any(other.startswith(name + ".") for other in modules)


def _name_in_module(modules: dict[str, ast.Module], module: str, name: str) -> bool:
    if _module_binds(modules.get(module), name):
        return True
    return _module_present(modules, f"{module}.{name}")


def _names_nearby(modules: dict[str, ast.Module], module: str) -> list[str]:
    tree = modules.get(module)
    if tree is None:
        return _child_modules(modules, module)[:_MAX_CANDIDATES]
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
    return sorted(dict.fromkeys(names))[:_MAX_CANDIDATES]


def _child_modules(modules: dict[str, ast.Module], parent: str) -> list[str]:
    prefix = parent + "."
    children: set[str] = set()
    for name in modules:
        if name.startswith(prefix):
            rest = name[len(prefix) :].split(".", 1)[0]
            if rest and not rest.startswith("_"):
                children.add(rest)
    return sorted(children)


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
    nearby = _child_modules(modules, parent) if parent else _child_modules(modules, missing.split(".", 1)[0])
    if parent:
        nearby = list(dict.fromkeys(nearby + _names_nearby(modules, parent)))[:_MAX_CANDIDATES]
    where = parent or missing.split(".", 1)[0]
    return (
        f"{path}:{line} {label}: {missing} {DOES_NOT_EXIST} {package} {version}"
        + (f" (names in {where}: {', '.join(nearby)})" if nearby else "")
    )
