"""Parse currently installed package versions from manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conduit.prune.grep_imports import SKIP_DIRS

# Flatten order: earlier wins. npm last so it cannot overwrite a PyPI pin.
_FLATTEN_ECOSYSTEMS = ("pypi", "go", "maven", "npm")

_PACKET_ECOSYSTEM = {
    "pypi": "pypi",
    "pip": "pypi",
    "pyproject": "pypi",
    "npm": "npm",
    "go": "go",
    "maven": "maven",
}


def normalize_packet_ecosystem(value: str | None) -> str | None:
    return _PACKET_ECOSYSTEM.get(str(value or "").strip().lower())


def _path_skipped(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in SKIP_DIRS for part in rel_parts)


def _depth_key(path: Path, root: Path) -> tuple[int, str]:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return (10_000, path.as_posix().lower())
    return (len(rel.parts), rel.as_posix().lower())


def iter_pip_manifests(root: Path, *, scope: str = "main") -> list[Path]:
    """Root plus nested requirements/constraints files (venv/vendor skipped).

    Results are ordered shallowest-first so callers can prefer root pins.
    """
    root = root.resolve()
    names = (
        {"requirements-dev.txt"}
        if scope == "dev"
        else {"requirements.txt", "constraints.txt"}
    )
    name_set = {n.lower() for n in names}
    found: list[Path] = []
    seen: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file() or _path_skipped(path, root):
            continue
        name = path.name.lower()
        if name in name_set or (
            scope != "dev"
            and name.endswith(".txt")
            and "requirements" in name
            and name != "requirements-dev.txt"
        ):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(path)
    found.sort(key=lambda p: _depth_key(p, root))
    return found


def _parse_requirements_lines(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        matched = re.match(r"^([A-Za-z0-9_.\-]+)\s*([=<>!~]+)\s*([^\s;#]+)", line)
        if matched:
            out[matched.group(1).lower()] = matched.group(3).strip()
            continue
        matched = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)", line)
        if matched:
            out[matched.group(1).lower()] = matched.group(2).strip()
    return out


def _parse_package_json_deps(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(key) or {}
        if isinstance(block, dict):
            for name, ver in block.items():
                out[str(name).lower()] = str(ver).lstrip("^~>=< ")
    return out


def _parse_pyproject_deps(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for matched in re.finditer(
        r'["\']([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?[=<>!~]+([^"\']+)["\']',
        text,
    ):
        out[matched.group(1).lower()] = matched.group(2).strip()
    for matched in re.finditer(
        r'^([A-Za-z0-9_.\-]+)\s*=\s*["\']([^"\']+)["\']',
        text,
        re.MULTILINE,
    ):
        name = matched.group(1).lower()
        if name in {"name", "version", "description", "readme", "requires-python"}:
            continue
        ver = matched.group(2).strip()
        if re.match(r"^[\d*]", ver) or ver.startswith("^") or ver.startswith("~"):
            out[name] = ver.lstrip("^~>=< ")
    return out


def _parse_go_mod(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for matched in re.finditer(r"^\s*([^\s]+)\s+v([0-9][^\s]+)", text, re.MULTILINE):
        out[matched.group(1).lower()] = matched.group(2)
    return out


def read_installed_by_ecosystem(root: Path) -> dict[str, dict[str, str]]:
    """Return ecosystem -> package -> version from manifests."""
    root = root.resolve()
    by_eco: dict[str, dict[str, str]] = {}

    def _put(eco: str, mapping: dict[str, str], *, overwrite: bool = True) -> None:
        bucket = by_eco.setdefault(eco, {})
        for name, ver in mapping.items():
            key = str(name).lower()
            val = str(ver or "").strip()
            if not key or not val:
                continue
            if overwrite or key not in bucket:
                bucket[key] = val

    for req in iter_pip_manifests(root, scope="main"):
        try:
            text = req.read_text(encoding="utf-8")
        except OSError:
            continue
        _put("pypi", _parse_requirements_lines(text), overwrite=False)

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        _put(
            "pypi",
            _parse_pyproject_deps(pyproject.read_text(encoding="utf-8")),
            overwrite=False,
        )

    pkg = root / "package.json"
    if pkg.is_file():
        _put("npm", _parse_package_json_deps(pkg.read_text(encoding="utf-8")))

    gomod = root / "go.mod"
    if gomod.is_file():
        _put("go", _parse_go_mod(gomod.read_text(encoding="utf-8")))

    return by_eco


def flatten_installed(by_eco: dict[str, dict[str, str]]) -> dict[str, str]:
    """One version per package name; PyPI/go win over npm for mixed repos."""
    installed: dict[str, str] = {}
    for eco in _FLATTEN_ECOSYSTEMS:
        for name, ver in (by_eco.get(eco) or {}).items():
            installed.setdefault(name.lower(), ver)
    return installed


def read_installed(root: Path) -> dict[str, str]:
    """Return package -> version from manifests, including nested requirement files."""
    return flatten_installed(read_installed_by_ecosystem(root))


def pin_for_packet_ecosystem(
    by_eco: dict[str, dict[str, str]],
    package: str,
    ecosystem: str | None,
) -> str | None:
    """Client pin for ``package`` in one ecosystem, or None."""
    pkg = str(package or "").strip().lower()
    if not pkg:
        return None
    eco = normalize_packet_ecosystem(ecosystem)
    if eco is None:
        return flatten_installed(by_eco).get(pkg)
    ver = str((by_eco.get(eco) or {}).get(pkg) or "").strip()
    return ver or None
