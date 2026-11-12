"""Target-version SDK names: ground the model, then reject invented ones.

Before a rewrite, ``ground_candidates`` ranks a short list of real public names
from the target wheel (modules, classes, functions, methods) using the same
token / parameter overlap signals as impact candidates — no vendor tables.

After a rewrite, ``name_breaks`` checks new or used SDK imports and
module-rooted attribute chains against that wheel. One corrective retry is
allowed; a second miss stays needs-human and is never verified.

Surface extraction reuses ``outside_in.ast_checks`` single-pass walkers so this
does not add a parallel AST walker over the release modules.
"""

from __future__ import annotations

import ast

from conduit.apply.typecheck import _module_binds
from conduit.impact.candidates import pick_candidates
from conduit.outside_in.ast_checks import AstCheck, run_checks

#: Cap for prompt grounding and for "nearby names" in findings.
MAX_GROUND_CANDIDATES = 16
_MAX_NEARBY = 12
_MAX_SIGNATURE_CHARS = 160
#: Distinctive phrase so the apply engine can hand off after one name retry.
DOES_NOT_EXIST = "does not exist in"


def ground_candidates(
    modules: dict[str, ast.Module],
    *,
    package: str,
    hints: list[str],
    old_params: list[str] | None = None,
    call_kwargs: list[str] | None = None,
    limit: int = MAX_GROUND_CANDIDATES,
) -> list[dict[str, str]]:
    """Short list of real target-version names near ``hints``.

    Builds the public surface once from ``modules``, then ranks with
    ``pick_candidates`` (token similarity, leaf match, parameter overlap).
    """
    top = _top(package)
    if not top or not modules:
        return []
    surface = public_surface(modules, top)
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
) -> list[str]:
    """Findings for SDK imports and module-rooted attribute chains the rewrite uses.

    Checks:
    - ``import pkg...`` / ``from pkg... import name`` that the rewrite introduced,
      or whose local name is used inside the rewritten span — or, when
      ``all_imports`` is True (patched-file recheck), **every** package import
      in the file, including stale leftovers the rewrite left behind
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
    for part in parts:
        # Prefer a submodule when both a binding and a submodule exist.
        child = f"{current}.{part}"
        if _module_present(modules, child):
            current = child
            continue
        if _name_in_module(modules, current, part):
            # A value (class/function/constant): further attributes are not module paths.
            # Typed attribute checks cover fields on release classes.
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
                if len(nearby) >= _MAX_NEARBY:
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
