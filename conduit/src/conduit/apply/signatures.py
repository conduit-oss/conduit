"""New-release signatures read from the new wheel's own code (for prompts).

Only the wheel decides what a callable takes: parameter names, defaults and
annotations as source text, plus TypedDict parameter types it names.
"""

from __future__ import annotations

import ast
import zipfile
from functools import lru_cache
from pathlib import Path

_MAX_SIGNATURE = 2500
_MAX_TYPES = 4
_MAX_TYPE = 500
_MAX_FIELD = 100


def new_release_wheel(
    package: str, version: str, *, fixtures: Path | None = None, allow_network: bool = False
) -> Path | None:
    """The recorded or downloaded wheel for ``package==version``."""
    from conduit.impact.run import _artifact, _download_wheel, _load_index

    if not package or not version:
        return None
    entry = _load_index(fixtures).get(package.lower()) or {}
    wheels = entry.get("wheels") if isinstance(entry.get("wheels"), dict) else {}
    found = _artifact(fixtures, wheels.get(version))
    if found is None and allow_network:
        found = _download_wheel(package, version)
    return found


def signatures_for(wheel: Path | None, symbols: list[str]) -> dict[str, str]:
    """``symbol -> text`` for each symbol the wheel defines as a function or method."""
    if wheel is None:
        return {}
    modules = _modules(str(wheel))
    out: dict[str, str] = {}
    for symbol in symbols:
        if symbol and symbol not in out:
            text = _signature_text(modules, symbol)
            if text:
                out[symbol] = text
    return out


@lru_cache(maxsize=8)
def _modules(wheel: str) -> dict[str, ast.Module]:
    path = Path(wheel)
    sources: dict[str, str] = {}
    if path.is_dir():
        for item in path.rglob("*.py"):
            sources[item.relative_to(path).as_posix()] = item.read_text(encoding="utf-8", errors="replace")
    else:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".py"):
                    sources[name] = archive.read(name).decode("utf-8", errors="replace")
    modules: dict[str, ast.Module] = {}
    for name, text in sources.items():
        dotted = name[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        try:
            modules[dotted] = ast.parse(text)
        except SyntaxError:
            continue
    return modules


def _signature_text(modules: dict[str, ast.Module], symbol: str) -> str:
    parts = symbol.split(".")
    resolved = _resolve_callable(modules, symbol)
    if resolved is not None:
        return _render(modules, resolved, parts)
    found: list[tuple[int, str, ast.AST, ast.Module]] = []
    # ``Class.method`` is looked up as a method only; ``module.func`` as a function.
    lengths = (2,) if len(parts) >= 2 and parts[-2][:1].isupper() else (1,)
    for owner_len in lengths:
        if len(parts) <= owner_len:
            continue
        tail = parts[-owner_len:]
        for module_name, tree in modules.items():
            node = _lookup(tree, tail)
            if node is None:
                continue
            shared = _shared_prefix(module_name.split("."), parts)
            found.append((shared, module_name, node, tree))
        if found:
            break
    if not found:
        return ""
    found.sort(key=lambda item: (-item[0], item[1]))
    _shared, _module, node, _tree = found[0]
    return _render(modules, node, parts)


def _render(modules: dict[str, ast.Module], node: ast.AST, parts: list[str]) -> str:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    text = f"{'.'.join(parts[-2:])}({ast.unparse(node.args)})"
    if node.returns is not None:
        text += f" -> {ast.unparse(node.returns)}"
    text = text[:_MAX_SIGNATURE]
    types = _typed_dicts(modules, node)
    if types:
        text += "\nParameter types:\n" + "\n".join(types)
    return text


def _lookup(tree: ast.Module, tail: list[str]) -> ast.AST | None:
    scope: list[ast.stmt] = list(tree.body)
    for index, name in enumerate(tail):
        last = index == len(tail) - 1
        matches = [
            node
            for node in scope
            if getattr(node, "name", None) == name
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef) if last else ast.ClassDef)
        ]
        if not matches:
            return None
        if last:
            # The implementation, not an ``@overload`` stub, when both exist.
            plain = [node for node in matches if not _is_overload(node)]
            return (plain or matches)[-1]
        scope = list(matches[0].body)
    return None


def _is_overload(node: ast.AST) -> bool:
    for deco in getattr(node, "decorator_list", []):
        name = deco.attr if isinstance(deco, ast.Attribute) else getattr(deco, "id", "")
        if name == "overload":
            return True
    return False


def _shared_prefix(left: list[str], right: list[str]) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def _typed_dicts(modules: dict[str, ast.Module], node: ast.AST) -> list[str]:
    """TypedDict classes named in the parameter annotations, as field lists."""
    args = node.args  # type: ignore[attr-defined]
    names: list[str] = []
    positional = [*args.posonlyargs, *args.args]
    required = positional[: len(positional) - len(args.defaults)]
    required += [arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None]
    # Types of required parameters first: those are what a rewrite must build.
    ordered = required + [arg for arg in (*positional, *args.kwonlyargs) if arg not in required]
    for arg in ordered:
        if arg.annotation is None:
            continue
        for sub in ast.walk(arg.annotation):
            if isinstance(sub, ast.Name) and sub.id not in names:
                names.append(sub.id)
    out: list[str] = []
    for name in names:
        if len(out) >= _MAX_TYPES:
            break
        klass = _typed_dict_class(modules, name)
        if klass is None:
            continue
        fields = [
            f"{stmt.target.id}: {ast.unparse(stmt.annotation)[:_MAX_FIELD]}"
            for stmt in klass.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        ]
        out.append(f"{name}(TypedDict): " + "; ".join(fields)[:_MAX_TYPE])
    return out


def _typed_dict_class(modules: dict[str, ast.Module], name: str) -> ast.ClassDef | None:
    for tree in modules.values():
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                bases = [ast.unparse(base) for base in node.bases]
                if any(base.split(".")[-1] == "TypedDict" for base in bases):
                    return node
    return None


def unknown_keywords(wheel: Path | None, call_target: str, keywords: tuple[str, ...]) -> tuple[str, list[str]]:
    """``(callable, kwargs it does not accept)`` for a call into the new release.

    ``call_target`` is the call's resolved name (``pkg.Client.attr.method``).
    Attributes are followed through property / cached_property return types
    and class annotations in the new wheel. A callable with ``**kwargs``
    accepts every keyword. Unresolved targets report nothing.
    """
    if wheel is None or not keywords:
        return "", []
    modules = _modules(str(wheel))
    node = _resolve_callable(modules, call_target)
    if node is None:
        return "", []
    args = node.args
    if args.kwarg is not None:
        return "", []
    accepted = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    owner = ".".join(call_target.split(".")[-2:])
    return owner, [name for name in keywords if name and name not in accepted]


def _resolve_callable(modules: dict[str, ast.Module], dotted: str) -> ast.AST | None:
    found = resolve_callable(modules, dotted)
    return found[1] if found else None


def resolve_callable(
    modules: dict[str, ast.Module], dotted: str
) -> tuple[str, ast.AST] | None:
    """``(defining module, def node)`` for a call's resolved name in the wheel."""
    parts = [part for part in dotted.split(".") if part]
    for index in range(len(parts) - 1, 0, -1):
        if not parts[index][:1].isupper():
            continue
        klass = _find_class(modules, parts[index], ".".join(parts[:index]))
        if klass is None:
            continue
        found = _walk_members(modules, klass, parts[index + 1 :])
        if found is not None:
            return found
    return None


def _find_class(
    modules: dict[str, ast.Module], name: str, near: str
) -> tuple[str, ast.ClassDef] | None:
    """The class ``name``: imported into module ``near`` if possible, else closest module."""
    tree = modules.get(near)
    if tree is not None:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return near, node
        # Imports anywhere in the module, including ``if TYPE_CHECKING:`` blocks.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (alias.asname or alias.name) == name:
                        source = _absolute(near, node, is_package=_is_package(modules, near))
                        if source != near and source in modules:
                            hit = _find_class(modules, alias.name, source)
                            if hit is not None:
                                return hit
    best: tuple[int, str, ast.ClassDef] | None = None
    for module_name, module in modules.items():
        for node in module.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                score = _shared_prefix(module_name.split("."), near.split("."))
                if best is None or score > best[0]:
                    best = (score, module_name, node)
    return (best[1], best[2]) if best else None


def _walk_members(
    modules: dict[str, ast.Module], klass: tuple[str, ast.ClassDef], rest: list[str]
) -> tuple[str, ast.AST] | None:
    module_name, node = klass
    for index, attr in enumerate(rest):
        last = index == len(rest) - 1
        member = _member(modules, module_name, node, attr)
        if member is None:
            return None
        if last:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return _owner_module(modules, module_name, node, attr), member
            return None
        annotation = member.returns if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) else member.annotation
        type_name = _type_name(annotation)
        hit = _find_class(modules, type_name, module_name) if type_name else None
        if hit is None:
            return None
        module_name, node = hit
    return None


def _owner_module(modules, module_name: str, node: ast.ClassDef, attr: str, depth: int = 0) -> str:
    """Module of the class (or base) that defines ``attr``; names in its annotations resolve there."""
    if any(getattr(child, "name", None) == attr for child in node.body) or depth >= 3:
        return module_name
    for base in node.bases:
        name = _type_name(base)
        hit = _find_class(modules, name, module_name) if name else None
        if hit is not None and hit[1] is not node and _member(modules, hit[0], hit[1], attr) is not None:
            return _owner_module(modules, hit[0], hit[1], attr, depth + 1)
    return module_name


def _member(modules, module_name: str, node: ast.ClassDef, attr: str, depth: int = 0):
    funcs = [
        child
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == attr
    ]
    if funcs:
        plain = [child for child in funcs if not _is_overload(child)]
        return (plain or funcs)[-1]
    for child in node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.target.id == attr:
            return child
    if depth >= 3:
        return None
    for base in node.bases:
        name = _type_name(base)
        hit = _find_class(modules, name, module_name) if name else None
        if hit is not None and hit[1] is not node:
            found = _member(modules, hit[0], hit[1], attr, depth + 1)
            if found is not None:
                return found
    return None


def _type_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return ""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_package(modules: dict[str, ast.Module], name: str) -> bool:
    prefix = name + "."
    return any(other.startswith(prefix) for other in modules)


def _absolute(module: str, node: ast.ImportFrom, *, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)
