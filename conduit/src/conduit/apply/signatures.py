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
    _shared, _module, node, tree = found[0]
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
