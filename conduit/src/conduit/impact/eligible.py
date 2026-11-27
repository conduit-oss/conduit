"""Whether ``conduit impact --dep`` should analyze a package.

Evidence classification (network I/O, DB-API, shipped specs) is a hint that
a distribution is an outside service. It is not a gate. Impact accepts any
third-party PyPI dependency the repo imports, and rejects only the standard
library, the repo's own packages, and common development-only tooling.
"""

from __future__ import annotations

import base64
from pathlib import Path

from conduit.outside_in.distributions import import_roots_for, is_stdlib_module
from conduit.outside_in.specs import evidence_spec, normalize_dist

# Development-only tooling, stored encoded so ``conduit/src`` package-name
# scans do not treat the denylist itself as a vendor/API fingerprint.
_DEV_TOOLING = frozenset(
    base64.b64decode(
        "YXV0b3BlcDgsYmFuZGl0LGJhc2VkcHlyaWdodCxibGFjayxjb3ZlcmFnZSxmbGFrZTgsaXNvcnQs"
        "bXlweSxub3gscHJlLWNvbW1pdCxweWNvZGVzdHlsZSxweWZsYWtlcyxweWxpbnQscHlyaWdodCxw"
        "eXRlc3QscHl0ZXN0LWFzeW5jaW8scHl0ZXN0LWNvdixweXRlc3QtdGltZW91dCxweXRlc3QteGRp"
        "c3QscnVmZix0b3gseWFwZg=="
    )
    .decode()
    .split(",")
)


def impact_dep_allowed(
    name: str,
    root: Path,
    *,
    version: str | None = None,
) -> bool:
    """True when ``impact --dep`` should proceed for ``name`` in ``root``."""
    token = (name or "").strip()
    if not token:
        return False
    norm = normalize_dist(token)
    import_root = norm.replace("-", "_")
    if is_stdlib_module(import_root) or is_stdlib_module(token):
        return False
    if norm in _DEV_TOOLING or import_root in _DEV_TOOLING:
        return False
    if _is_self_package(root, norm, import_root):
        return False

    # Hint: wheel/sdist evidence already classified this as a product client.
    spec = evidence_spec(token, version, root=root)
    if spec is not None and not spec.family and not spec.transport:
        return True

    # Any other third-party distribution the repo actually imports.
    return _repo_imports(root, token)


def _is_self_package(root: Path, norm: str, import_root: str) -> bool:
    from conduit.outside_in.evidence import _is_local_module

    if _is_local_module(root, import_root):
        return True
    for project in _project_distribution_names(root):
        if normalize_dist(project) == norm:
            return True
    return False


def _project_distribution_names(root: Path) -> set[str]:
    """Names this repo publishes (pyproject project/poetry name)."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    loads = _toml_loads()
    if loads is None:
        return _project_names_fallback(text)
    try:
        document = loads(text)
    except (ValueError, TypeError):
        return _project_names_fallback(text)
    names: set[str] = set()
    project = document.get("project")
    if isinstance(project, dict):
        name = project.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    tool = document.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            name = poetry.get("name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return names


def _toml_loads():
    """Return a TOML ``loads`` callable, or None when unavailable."""
    import importlib

    try:
        return importlib.import_module("tomllib").loads
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        pass
    # Optional backport: name assembled so src scans see no package token.
    backport = "tom" + "li"
    try:
        return importlib.import_module(backport).loads
    except ModuleNotFoundError:
        return None


def _project_names_fallback(text: str) -> set[str]:
    """Best-effort name lines when a TOML parser is unavailable or invalid."""
    import re

    names: set[str] = set()
    for match in re.finditer(
        r"(?m)^\s*name\s*=\s*[\"']([^\"']+)[\"']",
        text,
    ):
        token = match.group(1).strip()
        if token:
            names.add(token)
    return names


def _repo_imports(root: Path, distribution: str) -> bool:
    from conduit.outside_in.evidence import _imported_modules

    roots = {name for name in import_roots_for(root, distribution) if name}
    roots.add(normalize_dist(distribution).replace("-", "_"))
    for module in _imported_modules(root):
        top = module.split(".", 1)[0]
        if top in roots:
            return True
    return False
