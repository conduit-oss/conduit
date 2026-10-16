"""Third-party distributions a repo declares, and the names it imports.

Sources are requirements files, pyproject dependencies, lockfiles, and
installed metadata (a repo virtualenv's ``*.dist-info``, plus the active
environment's import map). Nothing here is a list of SDK product names.
The standard library is not a distribution we classify.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from conduit.deps.manifests import iter_pip_manifests
from conduit.outside_in.specs import ServiceSpec, evidence_spec, normalize_dist

_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | {"__future__", "builtins"}
_VENV_DIRS = (".venv", "venv")


def is_stdlib_module(name: str) -> bool:
    root = (name or "").split(".", 1)[0].strip()
    return bool(root) and root in _STDLIB


def declared_distribution_names(root: Path) -> list[str]:
    """Distribution names from requirements, pyproject, lockfiles, and a repo venv.

    Order is requirements, then pyproject, then lockfiles, then installed
    metadata. The first spelling of a normalized name wins.
    """
    root = root.resolve()
    found: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        token = (name or "").strip()
        if not token or token.startswith(("-", ".", "/")) or "://" in token:
            return
        norm = normalize_dist(token)
        if not norm or norm == "python" or norm in seen:
            return
        seen.add(norm)
        found.append(token)

    for path in iter_pip_manifests(root, scope="main"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for name in _requirement_names(text):
            add(name)

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for name in _pyproject_names(text):
            add(name)

    for name in ("poetry.lock", "uv.lock"):
        path = root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for item in _lock_names(text):
                add(item)

    pipfile = root / "Pipfile.lock"
    if pipfile.is_file():
        try:
            text = pipfile.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for item in _pipfile_names(text):
            add(item)

    for dist_name, _roots in _venv_distributions(root):
        add(dist_name)
    return found


def import_roots_for(root: Path, distribution: str) -> tuple[str, ...]:
    """Top-level import names for one distribution.

    Installed metadata wins when the import name is not the distribution
    name (``PIL`` for ``pillow``). Otherwise the normalized name with
    hyphens turned into underscores is the import root. Stdlib roots are dropped.
    """
    norm = normalize_dist(distribution)
    if not norm:
        return ()
    roots: list[str] = []
    for imported, dist in _import_index(root).items():
        if dist == norm and imported not in roots:
            roots.append(imported)
    for candidate in (norm.replace("-", "_"), re.sub(r"[-.]+", "_", distribution.strip())):
        if candidate.isidentifier() and candidate not in roots:
            roots.append(candidate)
    cleaned = tuple(name for name in roots if name.isidentifier() and not is_stdlib_module(name))
    return cleaned


def merge_declared_specs(
    root: Path, specs: list[ServiceSpec]
) -> tuple[list[ServiceSpec], set[str]]:
    """Append a spec for each declared distribution evidence does not already classify.

    The returned set is those added service ids. Callers drop them again when
    no call, constructor, or other chain reaches them.
    """
    covered: set[str] = set()
    for spec in specs:
        for token in (spec.package, spec.service_id):
            norm = normalize_dist(token)
            if norm:
                covered.add(norm)
    generic: set[str] = set()
    merged = list(specs)
    for name in declared_distribution_names(root):
        if evidence_spec(name) is not None:
            continue
        norm = normalize_dist(name)
        if not norm or norm in covered:
            continue
        modules = import_roots_for(root, name)
        if not modules:
            continue
        merged.append(
            ServiceSpec(
                service_id=norm,
                package=name,
                modules=modules,
            )
        )
        covered.add(norm)
        generic.add(norm)
    return merged, generic


def expand_unknown_spec(root: Path, spec: ServiceSpec) -> ServiceSpec:
    """Fill import roots for an explicit service evidence does not classify.

    ``--service some-dist`` has to see ``import some_dist``. Specs that
    already name their modules keep those modules.
    """
    if spec.family or spec.module_prefixes:
        return spec
    if evidence_spec(spec.service_id) is not None or evidence_spec(spec.package) is not None:
        return spec
    extra = import_roots_for(root, spec.package or spec.service_id)
    if not extra:
        return spec
    modules = tuple(dict.fromkeys((*spec.modules, *extra)))
    cleaned = tuple(name for name in modules if name.isidentifier() or "." in name)
    if not cleaned or cleaned == spec.modules:
        return spec
    from dataclasses import replace

    return replace(spec, modules=cleaned)


def _requirement_names(text: str) -> list[str]:
    names: list[str] = []
    for raw in text.splitlines():
        body = raw.split("#", 1)[0].strip()
        if not body or body.startswith(("-", ".", "/")) or "://" in body:
            continue
        body = body.split(";", 1)[0].strip()
        body = body.split("@", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", body)
        if match:
            names.append(match.group(1))
    return names


def _pyproject_names(text: str) -> list[str]:
    document = _parse_toml(text)
    if document is None:
        return _requirement_names(text)
    names: list[str] = []
    project = document.get("project") if hasattr(document, "get") else None
    if isinstance(project, dict):
        for key in ("dependencies",):
            for item in project.get(key) or []:
                names.extend(_requirement_names(str(item)))
        optional = project.get("optional-dependencies") or {}
        if isinstance(optional, dict):
            for group in optional.values():
                for item in group or []:
                    names.extend(_requirement_names(str(item)))
    tool = document.get("tool") if hasattr(document, "get") else None
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict):
        names.extend(_poetry_dep_names(poetry.get("dependencies")))
        groups = poetry.get("group") or {}
        if isinstance(groups, dict):
            for group in groups.values():
                if isinstance(group, dict):
                    names.extend(_poetry_dep_names(group.get("dependencies")))
    return names


def _poetry_dep_names(block: object) -> list[str]:
    if not isinstance(block, dict):
        return []
    return [str(name) for name in block if str(name).strip().lower() != "python"]


def _lock_names(text: str) -> list[str]:
    document = _parse_toml(text)
    names: list[str] = []
    if document is not None:
        packages = document.get("package") if hasattr(document, "get") else None
        if isinstance(packages, list):
            for item in packages:
                if hasattr(item, "get"):
                    name = str(item.get("name") or "").strip()
                    if name:
                        names.append(name)
        if names:
            return names
    for match in re.finditer(r'(?m)^name\s*=\s*"([^"]+)"', text):
        names.append(match.group(1).strip())
    return names


def _pipfile_names(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    for section in ("default", "develop"):
        block = data.get(section)
        if isinstance(block, dict):
            names.extend(str(name) for name in block)
    return names


def _parse_toml(text: str) -> object | None:
    try:
        import tomlkit
    except ImportError:
        return None
    try:
        return tomlkit.parse(text)
    except Exception:
        return None


def _venv_distributions(root: Path) -> list[tuple[str, tuple[str, ...]]]:
    found: list[tuple[str, tuple[str, ...]]] = []
    for info in _dist_info_dirs(root):
        name = _dist_info_name(info)
        if not name:
            continue
        found.append((name, _top_level(info)))
    return found


_IMPORT_INDEX: dict[str, dict[str, str]] = {}


def _import_index(root: Path) -> dict[str, str]:
    """Top-level import name → normalized distribution.

    A repo virtualenv is recorded first so a fixture metadata directory
    beats whatever happens to be installed in the interpreter.
    """
    key = str(root.resolve())
    cached = _IMPORT_INDEX.get(key)
    if cached is not None:
        return cached
    index: dict[str, str] = {}
    for name, roots in _venv_distributions(root):
        norm = normalize_dist(name)
        if not norm:
            continue
        for imported in roots:
            index.setdefault(imported, norm)
    for imported, norm in _interpreter_import_index().items():
        index.setdefault(imported, norm)
    _IMPORT_INDEX[key] = index
    return index


def _interpreter_import_index() -> dict[str, str]:
    try:
        from importlib.metadata import packages_distributions
    except ImportError:
        return {}
    try:
        data = packages_distributions()
    except Exception:
        return {}
    index: dict[str, str] = {}
    for imported, dists in data.items():
        if not isinstance(imported, str) or not imported.isidentifier():
            continue
        if not dists:
            continue
        norm = normalize_dist(str(dists[0]))
        if norm:
            index.setdefault(imported, norm)
    return index


def _dist_info_dirs(root: Path) -> list[Path]:
    found: list[Path] = []
    for folder in _VENV_DIRS:
        lib = root / folder / "lib"
        if not lib.is_dir():
            continue
        try:
            found.extend(lib.glob("python*/site-packages/*.dist-info"))
            found.extend(lib.glob("python*/site-packages/*.egg-info"))
        except OSError:
            continue
    return found


def _dist_info_name(path: Path) -> str:
    for meta_name in ("METADATA", "PKG-INFO"):
        meta = path / meta_name
        if not meta.is_file():
            continue
        try:
            text = meta.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lower().startswith("name:"):
                return line.split(":", 1)[1].strip()
    stem = path.name
    for suffix in (".dist-info", ".egg-info"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    match = re.match(r"^(.*?)-(\d.*)$", stem)
    if match:
        return match.group(1)
    return stem


def _top_level(path: Path) -> tuple[str, ...]:
    top = path / "top_level.txt"
    if not top.is_file():
        return ()
    try:
        text = top.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    names: list[str] = []
    for line in text.splitlines():
        token = line.strip()
        if token.isidentifier() and token not in names:
            names.append(token)
    return tuple(names)
