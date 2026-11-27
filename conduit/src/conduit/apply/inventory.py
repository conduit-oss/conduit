"""Static public-API inventory of an installed target SDK.

Before a fix prompt, ``build_inventory`` walks the installed package with
``importlib.util.find_spec`` + AST over source files (no package code is
executed). The compact name list goes into the propose prompt. After a
rewrite, ``unknown_sdk_imports`` checks every import from that package
against the inventory; misses become feedback with ``difflib`` suggestions
and count as the apply ``imports`` failure stage once retries are exhausted.

When the package is not installed, the inventory is empty and callers keep
existing behaviour (no prompt section, no import gate).
"""

from __future__ import annotations

import ast
import difflib
import importlib.machinery
import importlib.util
from collections.abc import Iterable
from pathlib import Path

from conduit.impact.candidates import symbol_tokens

#: Cap for names shown in the prompt / used for matching.
MAX_INVENTORY = 300
#: How many near-miss suggestions to list per unknown name.
_MAX_SUGGESTIONS = 3
#: Distinctive stage token for needs-human / eval bucketing (import failures).
IMPORTS_STAGE = "imports"
#: Phrase included in feedback and final errors so retries and reports match.
IMPORTS_FAILURE = f"{IMPORTS_STAGE}: unknown names from"


def package_root(dependency: str) -> str:
    """Import root for a distribution name (``example-sdk`` → ``example_sdk``)."""
    return (dependency or "").strip().lower().replace("-", "_")


def build_inventory(
    dependency: str,
    *,
    related: Iterable[str] = (),
    limit: int = MAX_INVENTORY,
    search_path: Iterable[str | Path] | None = None,
) -> list[str]:
    """Public modules / classes / functions / constants for the installed SDK.

    Respects ``__all__`` when present, skips ``_private`` names, and ranks
    names that share tokens with ``related`` (call-site symbols) first.
    Returns ``[]`` when the package cannot be found.
    """
    top = package_root(dependency)
    if not top or not top.isidentifier():
        return []
    sources = _package_sources(top, search_path=search_path)
    if not sources:
        return []
    names = _public_names(top, sources)
    if not names:
        return []
    return _prioritize(names, related, limit=limit)


def unknown_sdk_imports(
    source: str,
    dependency: str,
    inventory: Iterable[str],
) -> list[str]:
    """Dotted names imported from the target SDK that are not in ``inventory``.

    Empty ``inventory`` means the gate is off (package missing): always ``[]``.
    """
    names = {item for item in inventory if item}
    top = package_root(dependency)
    if not names or not top:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    unknown: list[str] = []
    seen: set[str] = set()

    def add(target: str) -> None:
        if target and target not in names and target not in seen:
            seen.add(target)
            unknown.append(target)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _belongs(alias.name, top):
                    add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not _belongs(node.module or "", top):
                continue
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    add(module)
                    continue
                dotted = f"{module}.{alias.name}" if module else alias.name
                # Accept the fully-qualified name, a re-export on the package root,
                # or the bare leaf when the inventory listed it that way.
                if dotted in names or alias.name in names or f"{top}.{alias.name}" in names:
                    continue
                add(dotted)
    return unknown


def import_feedback(
    unknown: list[str],
    inventory: Iterable[str],
    *,
    dependency: str,
) -> str:
    """Retry / needs-human text listing bad names and nearest inventory matches."""
    top = package_root(dependency) or dependency or "the target SDK"
    pool = [item for item in inventory if item]
    lines = [
        f"{IMPORTS_FAILURE} {top}: "
        "import only names from target_sdk_inventory."
    ]
    for name in unknown:
        near = difflib.get_close_matches(name, pool, n=_MAX_SUGGESTIONS, cutoff=0.4)
        if not near:
            leaf = name.rsplit(".", 1)[-1]
            near = difflib.get_close_matches(leaf, pool, n=_MAX_SUGGESTIONS, cutoff=0.4)
            if not near:
                near = difflib.get_close_matches(
                    leaf, [item.rsplit(".", 1)[-1] for item in pool], n=_MAX_SUGGESTIONS, cutoff=0.4
                )
                near = [
                    next((item for item in pool if item.endswith("." + leaf) or item == leaf), leaf)
                    for leaf in near
                ]
        if near:
            lines.append(f"- {name} (did you mean: {', '.join(near)}?)")
        else:
            lines.append(f"- {name}")
    lines.append(
        f"ImportError: cannot import name {unknown[0]!r} from {top!r} "
        f"({IMPORTS_STAGE} stage)."
    )
    return "\n".join(lines)


def is_imports_failure(text: str) -> bool:
    """True when ``text`` is this gate's imports-stage failure (or a load ImportError)."""
    if not text:
        return False
    if IMPORTS_FAILURE in text or f"({IMPORTS_STAGE} stage)" in text:
        return True
    return any(
        token in text
        for token in ("ImportError", "ModuleNotFoundError", "cannot import name", "ERROR collecting")
    )


def _belongs(module: str, top: str) -> bool:
    return module == top or module.startswith(top + ".")


def _package_sources(
    top: str, *, search_path: Iterable[str | Path] | None
) -> dict[str, str]:
    """``dotted module -> source text`` for ``top`` and submodules, or ``{}``."""
    try:
        if search_path is None:
            spec = importlib.util.find_spec(top)
        else:
            paths = [str(Path(item)) for item in search_path]
            spec = importlib.machinery.PathFinder.find_spec(top, paths)
    except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
        return {}
    if spec is None:
        return {}
    roots: list[Path] = []
    if spec.submodule_search_locations:
        roots.extend(Path(item) for item in spec.submodule_search_locations)
    elif spec.origin and spec.origin not in {"built-in", "frozen"}:
        origin = Path(spec.origin)
        if origin.is_file():
            return {top: _read(origin)}
    if not roots:
        return {}
    out: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        init = root / "__init__.py"
        if init.is_file():
            out[top] = _read(init)
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if rel.name == "__init__.py":
                parts = list(rel.parts[:-1])
            else:
                parts = list(rel.with_suffix("").parts)
            # Skip private packages/modules (_internal, _helpers.py).
            if any(part.startswith("_") for part in parts):
                continue
            dotted = ".".join([top, *parts]) if parts else top
            out.setdefault(dotted, _read(path))
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _public_names(top: str, sources: dict[str, str]) -> set[str]:
    names: set[str] = set()
    for module, text in sources.items():
        names.add(module)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        exported = _all_names(tree)
        for node in _module_body(tree):
            if isinstance(node, ast.ClassDef):
                if _keep(node.name, exported):
                    names.add(f"{module}.{node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _keep(node.name, exported):
                    names.add(f"{module}.{node.name}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and _keep(target.id, exported):
                        names.add(f"{module}.{target.id}")
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if _keep(node.target.id, exported):
                    names.add(f"{module}.{node.target.id}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    if local != "*" and _keep(local, exported):
                        names.add(f"{module}.{local}")
        if exported is not None:
            for name in exported:
                if name and not name.startswith("_"):
                    names.add(f"{module}.{name}")
                    if module == top:
                        names.add(f"{top}.{name}")
    return names


def _keep(name: str, exported: tuple[str, ...] | None) -> bool:
    if not name or name.startswith("_"):
        return False
    if exported is None:
        return True
    return name in exported


def _all_names(tree: ast.Module) -> tuple[str, ...] | None:
    """``__all__`` string entries when assigned at module level; else ``None``."""
    for node in _module_body(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            return ()
        return tuple(
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
    return None


def _module_body(tree: ast.Module):
    stack = list(tree.body)
    while stack:
        node = stack.pop(0)
        if isinstance(node, ast.If):
            stack = list(node.body) + list(node.orelse) + stack
            continue
        yield node


def _prioritize(names: set[str], related: Iterable[str], *, limit: int) -> list[str]:
    hints = [item for item in related if item]
    hint_tokens: set[str] = set()
    for hint in hints:
        hint_tokens |= set(symbol_tokens(hint))
    scored: list[tuple[int, int, str]] = []
    for name in names:
        tokens = set(symbol_tokens(name))
        overlap = len(tokens & hint_tokens) if hint_tokens else 0
        # Prefer shorter public paths when scores tie (package root first).
        scored.append((-overlap, name.count("."), name))
    scored.sort()
    return [name for _score, _depth, name in scored[: max(0, limit)]]
