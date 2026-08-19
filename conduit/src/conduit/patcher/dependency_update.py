"""Update dependency pins in manifests (pip/npm/go/maven/gradle)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Array, Table

from conduit.prune.grep_imports import SKIP_DIRS

Op = Literal["bump", "add", "remove"]

DEP_RULE_TYPES = frozenset(
    {"DEPENDENCY_BUMP", "DEPENDENCY_ADD", "DEPENDENCY_REMOVE"}
)
_POETRY_SKIP = {"python", "python-versions"}


@dataclass
class DepEditResult:
    changed: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)


def rule_scope(rule: dict[str, Any]) -> str:
    raw = str(rule.get("scope") or "main").strip().lower()
    if raw in {"dev", "development", "optional"}:
        return "dev"
    if raw == "peer":
        return "peer"
    return "main"


def dependency_packages(packet: dict[str, Any]) -> list[str]:
    """Primary package plus every DEPENDENCY_* rule package (stable order)."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(name: Any) -> None:
        item = str(name or "").strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)

    _add(packet.get("package"))
    for rule in packet.get("rules") or []:
        if isinstance(rule, dict) and str(rule.get("type") or "") in DEP_RULE_TYPES:
            _add(rule.get("package"))
    return out


def format_pip_requirement(package: str, version: str) -> str:
    v = (version or "").strip()
    if not v:
        return package
    if v.startswith(("==", ">=", "<=", "~=", ">", "<")):
        return f"{package}{v}"
    if v.startswith("v") and len(v) > 1 and v[1].isdigit():
        v = v[1:]
    return f"{package}=={v}"


def format_caret_version(version: str) -> str:
    v = (version or "").strip()
    if not v:
        return v
    if v.startswith(("^", "~", ">=", "<=", ">", "<")):
        return v
    if v.startswith("=="):
        v = v[2:].strip()
    if v.startswith("v") and len(v) > 1 and v[1].isdigit():
        v = v[1:]
    return f"^{v}"


def format_go_version(version: str) -> str:
    v = (version or "").strip()
    if not v:
        return v
    if v.startswith("v"):
        return v
    return f"v{v}"


def _req_pattern(package: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?m)^(?P<lead>\s*){re.escape(package)}\s*(?:==|>=|~=|<=|>|<)?\s*[^\s#]*"
    )


def _edit_requirements_txt(
    path: Path,
    package: str,
    version: str,
    *,
    op: Op,
    dry_run: bool,
) -> bool:
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    pattern = _req_pattern(package)
    if op == "remove":
        updated, n = re.compile(
            rf"(?m)^\s*{re.escape(package)}\s*(?:==|>=|~=|<=|>|<)?[^\n]*\n?"
        ).subn("", original)
        if n == 0:
            return False
    elif pattern.search(original):
        spec = format_pip_requirement(package, version)

        def _repl(match: re.Match[str]) -> str:
            return f"{match.group('lead')}{spec}"

        updated, n = pattern.subn(_repl, original)
        if n == 0:
            return False
    elif op == "add":
        spec = format_pip_requirement(package, version)
        sep = "" if original.endswith("\n") or not original else "\n"
        updated = f"{original}{sep}{spec}\n"
    else:
        return False
    if updated == original:
        return op == "bump"
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def bump_requirements_txt(
    path: Path, package: str, to_version: str, *, dry_run: bool
) -> bool:
    return _edit_requirements_txt(
        path, package, to_version, op="bump", dry_run=dry_run
    )


def _npm_section(scope: str) -> str:
    if scope == "dev":
        return "devDependencies"
    if scope == "peer":
        return "peerDependencies"
    return "dependencies"


def _edit_package_json(
    path: Path,
    package: str,
    version: str,
    *,
    op: Op,
    scope: str,
    dry_run: bool,
) -> bool:
    if not path.is_file():
        return False
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    sections = ("dependencies", "devDependencies", "peerDependencies")
    existing: str | None = None
    for section in sections:
        deps = data.get(section)
        if isinstance(deps, dict) and package in deps:
            existing = section
            break
    pin = format_caret_version(version)
    if op == "remove":
        if existing is None:
            return False
        del data[existing][package]
        if not data[existing]:
            del data[existing]
    elif existing is not None:
        data[existing][package] = pin
    elif op == "add":
        section = _npm_section(scope)
        deps = data.get(section)
        if not isinstance(deps, dict):
            deps = {}
            data[section] = deps
        deps[package] = pin
    else:
        return False
    if not dry_run:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def bump_package_json(
    path: Path, package: str, to_version: str, *, dry_run: bool
) -> bool:
    return _edit_package_json(
        path, package, to_version, op="bump", scope="main", dry_run=dry_run
    )


def _edit_go_mod(
    path: Path,
    package: str,
    version: str,
    *,
    op: Op,
    dry_run: bool,
) -> bool:
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    ver = format_go_version(version)
    line_re = re.compile(
        rf"(?m)^((?:require\s+)?\s*{re.escape(package)}\s+)v?[^\s]+"
    )
    if op == "remove":
        updated, n = re.compile(
            rf"(?m)^(?:require\s+)?\s*{re.escape(package)}\s+v?[^\s]+\s*\n?"
        ).subn("", original)
        if n == 0:
            return False
    elif line_re.search(original):
        updated, n = line_re.subn(rf"\1{ver}", original)
        if n == 0:
            return False
    elif op == "add":
        sep = "" if original.endswith("\n") or not original else "\n"
        updated = f"{original}{sep}require {package} {ver}\n"
    else:
        return False
    if updated == original:
        return False
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def bump_go_mod(path: Path, package: str, to_version: str, *, dry_run: bool) -> bool:
    return _edit_go_mod(path, package, to_version, op="bump", dry_run=dry_run)


def bump_pom_xml(path: Path, package: str, to_version: str, *, dry_run: bool) -> bool:
    """Lightweight Maven pom.xml bump by artifactId + following version tag."""
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(<artifactId>\s*{re.escape(package)}\s*</artifactId>\s*"
        rf"(?:<(?!version\b)[^>]+>.*?</[^>]+>\s*)*"
        rf"<version>\s*)([^<]+)(</version>)",
        re.DOTALL,
    )
    updated, n = pattern.subn(rf"\g<1>{to_version}\3", original)
    if n == 0:
        artifact = package.split(":")[-1]
        if artifact != package:
            pattern = re.compile(
                rf"(<artifactId>\s*{re.escape(artifact)}\s*</artifactId>\s*"
                rf"(?:<(?!version\b)[^>]+>.*?</[^>]+>\s*)*"
                rf"<version>\s*)([^<]+)(</version>)",
                re.DOTALL,
            )
            updated, n = pattern.subn(rf"\g<1>{to_version}\3", original)
    if n == 0:
        return False
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def bump_gradle(path: Path, package: str, to_version: str, *, dry_run: bool) -> bool:
    """Bump Gradle dependency strings containing the package/coordinate."""
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    artifact = package if ":" in package else package
    pattern = re.compile(
        rf'(["\'])({re.escape(artifact)}):([^"\']*)(["\'])'
    )
    updated, n = pattern.subn(rf"\1\2:{to_version}\4", original)
    if n == 0 and ":" not in package:
        pattern = re.compile(
            rf'(["\'])([\w.-]+:{re.escape(package)}):([^"\']*)(["\'])'
        )
        updated, n = pattern.subn(rf"\1\2:{to_version}\4", original)
    if n == 0:
        return False
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def _table_key(table: Any, package: str) -> str | None:
    want = package.lower()
    if not isinstance(table, (Table, dict)):
        return None
    for key in list(table.keys()):
        if str(key).lower() == want:
            return str(key)
    return None


def _pep621_name(item: Any) -> str:
    text = str(item).strip().strip("\"'")
    match = re.match(r"^([A-Za-z0-9_.-]+)", text)
    return match.group(1).lower() if match else ""


def _set_poetry_dep(table: Any, package: str, version: str, *, op: Op) -> bool:
    if package.lower() in _POETRY_SKIP:
        return False
    key = _table_key(table, package)
    pin = format_caret_version(version)
    if op == "remove":
        if key is None:
            return False
        del table[key]
        return True
    if key is None:
        if op != "add":
            return False
        table[package] = pin
        return True
    current = table[key]
    if isinstance(current, (Table, dict)):
        current["version"] = pin
        return True
    try:
        current["version"] = pin
        return True
    except Exception:
        table[key] = pin
        return True


def _set_pep621_array(array: Array, package: str, version: str, *, op: Op) -> bool:
    spec = format_pip_requirement(package, version)
    want = package.lower()
    for i, item in enumerate(list(array)):
        if _pep621_name(item) != want:
            continue
        if op == "remove":
            del array[i]
            return True
        array[i] = spec
        return True
    if op == "add":
        array.append(spec)
        return True
    return False


def _poetry_dep_table(doc: Any, scope: str) -> Any | None:
    tool = doc.get("tool")
    if tool is None:
        return None
    poetry = tool.get("poetry")
    if poetry is None:
        return None
    if scope == "dev":
        group = poetry.get("group")
        if group is None:
            return None
        dev = group.get("dev")
        if dev is None:
            return None
        deps = dev.get("dependencies")
        return deps if isinstance(deps, (Table, dict)) else None
    if scope != "main":
        return None
    deps = poetry.get("dependencies")
    return deps if isinstance(deps, (Table, dict)) else None


def _pep621_arrays(doc: Any, scope: str) -> list[Array]:
    out: list[Array] = []
    project = doc.get("project")
    if scope == "main":
        if project is not None:
            deps = project.get("dependencies")
            if isinstance(deps, Array):
                out.append(deps)
        return out
    if scope != "dev":
        return out
    if project is not None:
        extras = project.get("optional-dependencies")
        if extras is not None:
            dev = extras.get("dev")
            if isinstance(dev, Array):
                out.append(dev)
    groups = doc.get("dependency-groups")
    if groups is not None:
        dev = groups.get("dev")
        if isinstance(dev, Array):
            out.append(dev)
    return out


def _edit_pyproject(
    path: Path,
    package: str,
    version: str,
    *,
    op: Op,
    scope: str,
    dry_run: bool,
) -> tuple[bool, str | None]:
    if not path.is_file():
        return False, None
    original = path.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(original)
    except TOMLKitError:
        return False, f"Skipped pyproject.toml: could not parse TOML"
    poetry = _poetry_dep_table(doc, scope)
    arrays = _pep621_arrays(doc, scope)
    if poetry is None and not arrays:
        if op == "add":
            label = "dev" if scope == "dev" else "main"
            return False, f"Skipped pyproject.toml: no {label} dependency table"
        return False, None
    changed = False
    if poetry is not None:
        changed = _set_poetry_dep(poetry, package, version, op=op) or changed
    for array in arrays:
        changed = _set_pep621_array(array, package, version, op=op) or changed
    if not changed:
        return False, None
    if not dry_run:
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return True, None


def bump_pyproject(
    path: Path, package: str, to_version: str, *, dry_run: bool
) -> bool:
    ok, _skip = _edit_pyproject(
        path, package, to_version, op="bump", scope="main", dry_run=dry_run
    )
    return ok


def _path_skipped(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in SKIP_DIRS for part in rel_parts)


def _iter_pip_manifests(root: Path, *, scope: str) -> list[Path]:
    """Root plus nested requirements/constraints files (venv/vendor skipped)."""
    names = (
        {"requirements-dev.txt"}
        if scope == "dev"
        else {"requirements.txt", "constraints.txt"}
    )
    found: list[Path] = []
    seen: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file() or _path_skipped(path, root):
            continue
        name = path.name.lower()
        if name in {n.lower() for n in names} or (
            scope != "dev"
            and name.endswith(".txt")
            and "requirements" in name
            and name != "requirements-dev.txt"
        ):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(path)
    return found


def _iter_npm_manifests(root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for path in root.rglob("package.json"):
        if not path.is_file() or _path_skipped(path, root):
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            found.append(path)
    return found


def apply_dependency_rule(
    root: Path,
    rule: dict[str, Any],
    *,
    dry_run: bool = False,
) -> DepEditResult:
    rtype = str(rule.get("type") or "")
    op: Op | None = {
        "DEPENDENCY_BUMP": "bump",
        "DEPENDENCY_ADD": "add",
        "DEPENDENCY_REMOVE": "remove",
    }.get(rtype)
    result = DepEditResult()
    if op is None:
        return result
    package = str(rule.get("package") or "").strip()
    version = str(rule.get("to_version") or rule.get("from_version") or "").strip()
    if not package:
        return result
    scope = rule_scope(rule)
    if rule.get("ecosystems"):
        ecosystems = set(rule["ecosystems"])
    elif op == "bump":
        ecosystems = {"pip", "npm", "pyproject", "go", "maven", "gradle"}
    else:
        ecosystems = {"pip", "pyproject"}

    def _rel(path: Path) -> str:
        return str(path.relative_to(root))

    if "pip" in ecosystems:
        pip_paths = _iter_pip_manifests(root, scope=scope)
        if scope == "dev" and not pip_paths:
            result.skips.append(
                "Skipped requirements-dev.txt: file does not exist"
            )
        for req in pip_paths:
            if _edit_requirements_txt(req, package, version, op=op, dry_run=dry_run):
                result.changed.append(_rel(req))

    if "pyproject" in ecosystems:
        pyproject = root / "pyproject.toml"
        ok, skip = _edit_pyproject(
            pyproject, package, version, op=op, scope=scope, dry_run=dry_run
        )
        if ok:
            result.changed.append(_rel(pyproject))
        elif skip:
            result.skips.append(skip)

    if "npm" in ecosystems:
        for pkg in _iter_npm_manifests(root):
            if _edit_package_json(
                pkg, package, version, op=op, scope=scope, dry_run=dry_run
            ):
                result.changed.append(_rel(pkg))

    if "go" in ecosystems:
        gomod = root / "go.mod"
        if scope == "dev":
            if gomod.is_file():
                result.skips.append("Skipped go.mod: scope 'dev' is not supported")
        elif _edit_go_mod(gomod, package, version, op=op, dry_run=dry_run):
            result.changed.append(_rel(gomod))

    if op == "bump" and "maven" in ecosystems:
        pom = root / "pom.xml"
        if bump_pom_xml(pom, package, version, dry_run=dry_run):
            result.changed.append(_rel(pom))
    elif op != "bump" and "maven" in ecosystems and (root / "pom.xml").is_file():
        result.skips.append(
            f"Skipped pom.xml: {rtype} is not supported for maven"
        )

    if op == "bump" and "gradle" in ecosystems:
        for name in ("build.gradle", "build.gradle.kts"):
            gradle = root / name
            if bump_gradle(gradle, package, version, dry_run=dry_run):
                result.changed.append(_rel(gradle))
    elif op != "bump" and "gradle" in ecosystems and any(
        (root / name).is_file()
        for name in ("build.gradle", "build.gradle.kts")
    ):
        result.skips.append(
            f"Skipped Gradle: {rtype} is not supported for gradle"
        )

    return result


def apply_dependency_bump(
    root: Path,
    rule: dict[str, Any],
    *,
    dry_run: bool = False,
) -> list[str]:
    return apply_dependency_rule(root, rule, dry_run=dry_run).changed
