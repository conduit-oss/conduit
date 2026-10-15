"""Resolve the version of an outside-service client when it is not pinned with ``==``.

Order: an exact pin, a lockfile (``poetry.lock``, ``uv.lock``, ``Pipfile.lock``),
the installed distribution (``importlib.metadata``, then ``site-packages``),
then a requirement range resolved to the highest match on PyPI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from conduit.detect.pip_manifests import iter_pip_manifests

_NAME = r"[A-Za-z0-9_.\-]+"


def resolve_client_version(
    root: Path,
    package: str,
    *,
    allow_network: bool,
) -> tuple[str, str]:
    """Return ``(version, reason)``.

    ``reason`` is empty when ``version`` is set. When nothing can be resolved,
    ``version`` is empty and ``reason`` says which sources were missing.
    """
    name = (package or "").strip()
    if not name:
        return "", "not compared: no package name to resolve"
    exact, ranges = _declared_specs(root, name)
    if exact:
        return exact, ""
    locked = _lock_version(root, name)
    if locked:
        return locked, ""
    installed = installed_version(name)
    if installed:
        return installed, ""
    if ranges:
        spec = ranges[0]
        if not allow_network:
            return "", (
                f"not compared: requirement {spec!r} for {name} needs PyPI to "
                "choose a version and network is disabled"
            )
        try:
            match = highest_on_pypi(name, spec)
        except Exception as exc:
            return "", (
                f"not compared: requirement {spec!r} for {name} could not be "
                f"resolved on PyPI ({exc})"
            )
        if match:
            return match, ""
        return "", (
            f"not compared: requirement {spec!r} for {name} matched no version on PyPI"
        )
    return "", (
        "not compared: no version could be determined "
        "(no exact pin, lockfile entry, installed distribution, or resolvable requirement)"
    )


def pypi_latest(package: str) -> str:
    """Current version PyPI publishes for ``package``, or empty."""
    meta = _pypi_json(package)
    info = meta.get("info") if isinstance(meta, dict) else None
    if isinstance(info, dict):
        version = str(info.get("version") or "").strip()
        if version:
            return version
    return ""


def highest_on_pypi(package: str, spec: str) -> str:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    try:
        wanted = SpecifierSet(spec)
    except InvalidSpecifier:
        return ""
    found: list[Version] = []
    for raw in _pypi_versions(package):
        try:
            parsed = Version(raw)
        except InvalidVersion:
            continue
        if parsed in wanted:
            found.append(parsed)
    if not found:
        return ""
    return str(max(found))


def installed_version(package: str) -> str:
    """Version from ``importlib.metadata``, then a ``*.dist-info`` on ``sys.path``."""
    found = _metadata_version(package)
    if found:
        return found
    return _site_packages_version(package)


def _metadata_version(package: str) -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return ""
    candidates = [package, package.replace("-", "_"), package.replace("_", "-")]
    seen: set[str] = set()
    for name in candidates:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        try:
            found = version(name)
        except PackageNotFoundError:
            continue
        except Exception:
            continue
        if found:
            return str(found).strip()
    return ""


def _site_packages_version(package: str) -> str:
    import sys

    target = package.lower().replace("-", "_")
    for entry in sys.path:
        root = Path(entry)
        if not root.is_dir():
            continue
        try:
            metas = list(root.glob("*.dist-info/METADATA"))
        except OSError:
            continue
        for meta in metas:
            name, version = _dist_info_identity(meta)
            if not name or not version:
                continue
            if name.lower().replace("-", "_") == target:
                return version
    return ""


def _dist_info_identity(path: Path) -> tuple[str, str]:
    name = ""
    version = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    for line in text.splitlines():
        if line.lower().startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.lower().startswith("version:"):
            version = line.split(":", 1)[1].strip()
        if name and version:
            break
    return name, version


def _declared_specs(root: Path, package: str) -> tuple[str, list[str]]:
    exact = ""
    ranges: list[str] = []
    root = root.resolve()
    for req in iter_pip_manifests(root, scope="main"):
        try:
            text = req.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            spec = _spec_from_requirement_line(line, package)
            if not spec:
                continue
            if _is_exact(spec) and not exact:
                exact = _exact_version(spec)
            elif spec not in ranges:
                ranges.append(spec)
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for spec in _pyproject_specs(text, package):
            if _is_exact(spec) and not exact:
                exact = _exact_version(spec)
            elif not _is_exact(spec) and spec not in ranges:
                ranges.append(spec)
    return exact, ranges


def _spec_from_requirement_line(line: str, package: str) -> str:
    body = line.split("#", 1)[0].strip()
    if not body or body.startswith("-"):
        return ""
    match = re.match(rf"(?i)^{re.escape(package)}(?:\[[^\]]+\])?\s*(.*)$", body)
    if match is None:
        return ""
    spec = match.group(1).split(";", 1)[0].strip()
    return spec


def _pyproject_specs(text: str, package: str) -> list[str]:
    found: list[str] = []
    escaped = re.escape(package)
    for match in re.finditer(
        rf"(?i)[\"']{escaped}(?:\[[^\]]+\])?\s*([^\"']*)[\"']",
        text,
    ):
        spec = match.group(1).strip()
        if spec and re.search(r"[=<>!~^]", spec):
            found.append(spec)
    for match in re.finditer(
        rf"(?im)^{escaped}\s*=\s*[\"']([^\"']+)[\"']",
        text,
    ):
        spec = match.group(1).strip()
        if spec:
            found.append(spec)
    return found


def _is_exact(spec: str) -> bool:
    token = spec.strip()
    if token.startswith("^") or token.startswith("~") and not token.startswith("=="):
        return False
    if "," in token or any(op in token for op in (">", "<", "!", "~")):
        return False
    if token.startswith("==="):
        return True
    if token.startswith("=="):
        return True
    return bool(re.match(r"^\d", token))


def _exact_version(spec: str) -> str:
    token = spec.strip()
    token = re.sub(r"^={2,3}", "", token).strip()
    return token


def _lock_version(root: Path, package: str) -> str:
    root = root.resolve()
    for name in ("poetry.lock", "uv.lock"):
        path = root / name
        if path.is_file():
            found = _toml_lock_version(path, package)
            if found:
                return found
    pipfile = root / "Pipfile.lock"
    if pipfile.is_file():
        return _pipfile_lock_version(pipfile, package)
    return ""


def _toml_lock_version(path: Path, package: str) -> str:
    try:
        import tomlkit
    except ImportError:
        return ""
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    wanted = package.lower().replace("_", "-")
    packages = doc.get("package")
    if isinstance(packages, list):
        for item in packages:
            if not hasattr(item, "get"):
                continue
            name = str(item.get("name") or "").lower().replace("_", "-")
            if name == wanted:
                version = str(item.get("version") or "").strip()
                if version:
                    return version
    return ""


def _pipfile_lock_version(path: Path, package: str) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    wanted = package.lower().replace("_", "-")
    if not isinstance(data, dict):
        return ""
    for section in ("default", "develop"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, meta in block.items():
            if str(name).lower().replace("_", "-") != wanted or not isinstance(meta, dict):
                continue
            raw = str(meta.get("version") or "").strip()
            if raw.startswith("=="):
                raw = raw[2:]
            return raw
    return ""


def _pypi_versions(package: str) -> list[str]:
    meta = _pypi_json(package)
    releases = meta.get("releases") if isinstance(meta, dict) else None
    if not isinstance(releases, dict):
        return []
    found: list[str] = []
    for version, files in releases.items():
        if not isinstance(files, list) or not files:
            continue
        if all(isinstance(item, dict) and item.get("yanked") for item in files):
            continue
        found.append(str(version))
    return found


def _pypi_json(package: str) -> dict:
    import httpx

    response = httpx.get(
        f"https://pypi.org/pypi/{package}/json",
        timeout=30.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return {}
    return payload
