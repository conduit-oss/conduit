"""Parse currently installed package versions from manifests."""

from __future__ import annotations

from pathlib import Path

from conduit.detect.lockfile_diff import (
    _parse_go_mod,
    _parse_package_json_deps,
    _parse_pyproject_deps,
    _parse_requirements_lines,
)
from conduit.detect.pip_manifests import iter_pip_manifests

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


def read_installed_by_ecosystem(root: Path) -> dict[str, dict[str, str]]:
    """Return ecosystem -> package -> version from manifests.

    Pip includes nested ``requirements*.txt`` / ``constraints.txt`` (shallowest
    pin wins). Root ``pyproject.toml`` / ``package.json`` / ``go.mod`` as today.
    Same package name can exist in pip and npm without one overwriting the other.
    """
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

    # Shallowest-first: first pin wins across nested requirements files.
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
    """Return package -> version from manifests (including nested pip requirements).

    When the same name is pinned in pip and npm, the PyPI pin is kept.
    Use ``read_installed_by_ecosystem`` / ``pin_for_packet_ecosystem`` when
    binding a catalog packet.
    """
    return flatten_installed(read_installed_by_ecosystem(root))


def pin_for_packet_ecosystem(
    by_eco: dict[str, dict[str, str]],
    package: str,
    ecosystem: str | None,
) -> str | None:
    """Client pin for ``package`` in the packet's ecosystem, or None.

    A pypi packet never receives an npm pin (and the reverse). Unknown /
    missing ecosystem falls back to the flattened (PyPI-first) map.
    """
    pkg = str(package or "").strip().lower()
    if not pkg:
        return None
    eco = normalize_packet_ecosystem(ecosystem)
    if eco is None:
        return flatten_installed(by_eco).get(pkg)
    ver = str((by_eco.get(eco) or {}).get(pkg) or "").strip()
    return ver or None
