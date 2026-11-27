"""Static public-API inventory of an installed target SDK.

Before a fix prompt, ``build_allowlist`` walks the installed package with
``importlib.util.find_spec`` + AST (no package code executed). ``build_inventory``
then trims that surface to names related to the call site (hard cap ~30 names /
~600 tokens) for the propose prompt. After a rewrite, ``unknown_sdk_imports``
checks every import from that package against the full allowlist; misses become
feedback with ``difflib`` suggestions and count as the apply ``imports`` failure
stage once retries are exhausted.

When the package is not installed, both lists are empty and callers keep
existing behaviour (no prompt section, no import gate).
"""

from __future__ import annotations

import ast
import difflib
import importlib.machinery
import importlib.util
import re
from collections.abc import Iterable
from pathlib import Path

from conduit.impact.candidates import symbol_tokens

#: Hard cap for names shown in the propose prompt.
MAX_INVENTORY = 30
#: Approximate token budget for the prompt inventory line (~4 chars/token).
MAX_INVENTORY_TOKENS = 600
#: How many near-miss suggestions to list per unknown name.
_MAX_SUGGESTIONS = 3
#: Distinctive stage token for needs-human / eval bucketing (import failures).
IMPORTS_STAGE = "imports"
#: Phrase included in feedback and final errors so retries and reports match.
IMPORTS_FAILURE = f"{IMPORTS_STAGE}: unknown names from"

_IMPORT_FROM = re.compile(
    r"^\s*from\s+([\w.]+)\s+import\s+(.+)$", re.MULTILINE
)
_IMPORT = re.compile(r"^\s*import\s+([\w.,\s]+)$", re.MULTILINE)


def package_root(dependency: str) -> str:
    """Import root for a distribution name (``example-sdk`` → ``example_sdk``)."""
    return (dependency or "").strip().lower().replace("-", "_")


def build_allowlist(
    dependency: str,
    *,
    search_path: Iterable[str | Path] | None = None,
) -> list[str]:
    """Full public surface of the installed SDK (modules / classes / functions / constants).

    Respects ``__all__`` when present and skips ``_private``. Returns ``[]`` when
    the package cannot be found. Intended for the import gate, not the prompt.
    """
    top = package_root(dependency)
    if not top or not top.isidentifier():
        return []
    sources = _package_sources(top, search_path=search_path)
    if not sources:
        return []
    return sorted(_public_names(top, sources))


def build_inventory(
    dependency: str,
    *,
    related: Iterable[str] = (),
    file_imports: str = "",
    limit: int = MAX_INVENTORY,
    max_tokens: int = MAX_INVENTORY_TOKENS,
    search_path: Iterable[str | Path] | None = None,
    allowlist: Iterable[str] | None = None,
) -> list[str]:
    """Per-site trimmed inventory for the propose prompt.

    Keeps names the file already imports from the SDK, the old call's module
    path / attributes, and target-SDK names in the same submodule or fuzzy-
    matched to those references. Hard-capped by ``limit`` and ``max_tokens``.
    """
    top = package_root(dependency)
    if not top or not top.isidentifier():
        return []
    if allowlist is None:
        names = set(build_allowlist(dependency, search_path=search_path))
    else:
        names = {item for item in allowlist if item}
    if not names:
        return []
    hints = list(related) + _hints_from_imports(file_imports, top)
    return _select_related(names, hints, top=top, limit=limit, max_tokens=max_tokens)


def format_inventory_line(inventory: Iterable[str]) -> str:
    """One-line prompt fragment; empty string when there is nothing to show."""
    names = [item for item in inventory if item]
    if not names:
        return ""
    return "target_sdk_inventory: " + ", ".join(names)


def inventory_token_estimate(inventory: Iterable[str]) -> int:
    """Rough token cost of the inventory line (charged at most once per site)."""
    line = format_inventory_line(inventory)
    if not line:
        return 0
    return max(1, (len(line) + 3) // 4)


def unknown_sdk_imports(
    source: str,
    dependency: str,
    allowlist: Iterable[str],
) -> list[str]:
    """Dotted names imported from the target SDK that are not in ``allowlist``.

    Empty ``allowlist`` means the gate is off (package missing): always ``[]``.
    """
    names = {item for item in allowlist if item}
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
                # or the bare leaf when the allowlist listed it that way.
                if dotted in names or alias.name in names or f"{top}.{alias.name}" in names:
                    continue
                add(dotted)
    return unknown


def import_feedback(
    unknown: list[str],
    inventory: Iterable[str],
    *,
    dependency: str,
    allowlist: Iterable[str] | None = None,
) -> str:
    """Retry / needs-human text listing bad names and nearest inventory matches."""
    top = package_root(dependency) or dependency or "the target SDK"
    pool = [item for item in inventory if item]
    if allowlist:
        # Prefer prompt inventory, then fill from the full allowlist for suggestions.
        extra = [item for item in allowlist if item and item not in pool]
        pool = pool + extra
    lines = [
        f"{IMPORTS_FAILURE} {top}: "
        "import only names from target_sdk_inventory (or real public names of that SDK)."
    ]
    for name in unknown:
        near = difflib.get_close_matches(name, pool, n=_MAX_SUGGESTIONS, cutoff=0.4)
        if not near:
            leaf = name.rsplit(".", 1)[-1]
            near = difflib.get_close_matches(leaf, pool, n=_MAX_SUGGESTIONS, cutoff=0.4)
            if not near:
                leaves = [item.rsplit(".", 1)[-1] for item in pool]
                leaf_hits = difflib.get_close_matches(
                    leaf, leaves, n=_MAX_SUGGESTIONS, cutoff=0.4
                )
                near = [
                    next(
                        (item for item in pool if item.endswith("." + hit) or item == hit),
                        hit,
                    )
                    for hit in leaf_hits
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


def _hints_from_imports(file_imports: str, top: str) -> list[str]:
    """Dotted SDK names referenced by the file's existing import lines."""
    if not file_imports or not top:
        return []
    out: list[str] = []
    for match in _IMPORT_FROM.finditer(file_imports):
        module = match.group(1).strip()
        if not _belongs(module, top):
            continue
        out.append(module)
        for part in match.group(2).split(","):
            token = part.strip().split(" as ", 1)[0].strip()
            if not token or token == "*":
                continue
            out.append(f"{module}.{token}")
    for match in _IMPORT.finditer(file_imports):
        for part in match.group(1).split(","):
            token = part.strip().split(" as ", 1)[0].strip()
            if _belongs(token, top):
                out.append(token)
    return out


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


def _select_related(
    names: set[str],
    related: Iterable[str],
    *,
    top: str,
    limit: int,
    max_tokens: int,
) -> list[str]:
    """Keep only names tied to the site's modules / attributes; cap size and tokens."""
    hints = [item.strip() for item in related if item and str(item).strip()]
    if not hints:
        # No site signal: omit the prompt section rather than dumping the SDK.
        return []

    modules: set[str] = {top}
    leaves: set[str] = set()
    exact: set[str] = set()
    hint_tokens: set[str] = set()
    for hint in hints:
        exact.add(hint)
        hint_tokens |= set(symbol_tokens(hint))
        parts = [part for part in hint.split(".") if part]
        if not parts:
            continue
        leaves.add(parts[-1])
        # Module path prefixes (acmekit.client.Client -> acmekit, acmekit.client).
        if _belongs(parts[0], top) or parts[0] == top:
            for depth in range(1, len(parts) + 1):
                prefix = ".".join(parts[:depth])
                if prefix in names or any(item.startswith(prefix + ".") for item in names):
                    modules.add(prefix)
                # Parent modules of a symbol path.
                if depth < len(parts):
                    modules.add(prefix)
        elif len(parts) == 1:
            leaves.add(parts[0])

    scored: list[tuple[int, int, str]] = []
    for name in names:
        score = _relatedness(name, top=top, modules=modules, leaves=leaves, exact=exact, hint_tokens=hint_tokens)
        if score <= 0:
            continue
        scored.append((-score, name.count("."), name))
    scored.sort()

    out: list[str] = []
    used_tokens = 0
    overhead = len("target_sdk_inventory: ")
    for _score, _depth, name in scored:
        # +2 for ", " separators after the first name.
        extra = len(name) + (2 if out else 0)
        estimate = (overhead + used_tokens + extra + 3) // 4
        if len(out) >= max(0, limit) or estimate > max_tokens:
            break
        out.append(name)
        used_tokens += extra
    return out


def _relatedness(
    name: str,
    *,
    top: str,
    modules: set[str],
    leaves: set[str],
    exact: set[str],
    hint_tokens: set[str],
) -> int:
    """Higher is more relevant; 0 means drop from the prompt inventory."""
    if name in exact:
        return 100
    leaf = name.rsplit(".", 1)[-1]
    score = 0
    # Same submodule / module path the site references.
    for module in modules:
        if name == module:
            score = max(score, 80)
        elif name.startswith(module + "."):
            rest = name[len(module) + 1 :]
            # Direct child of a referenced module beats deep unrelated cousins.
            score = max(score, 70 if "." not in rest else 50)
        elif module.startswith(name + ".") and _belongs(name, top):
            score = max(score, 60)
    if leaf in leaves:
        score = max(score, 65)
    elif leaves:
        # Fuzzy leaf match (old_call ~ send is weak; Client ~ ClientX is strong).
        for target in leaves:
            ratio = difflib.SequenceMatcher(None, leaf.lower(), target.lower()).ratio()
            if ratio >= 0.6:
                score = max(score, int(40 * ratio))
    tokens = set(symbol_tokens(name))
    overlap = len(tokens & hint_tokens) if hint_tokens else 0
    if overlap:
        score = max(score, 20 + 5 * overlap)
    # Always allow the package root when anything else matched.
    if name == top and score == 0 and (modules or leaves or exact):
        return 10
    return score
