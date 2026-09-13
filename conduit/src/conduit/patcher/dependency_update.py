"""Update dependency pins in manifests (pip/npm/go/maven/gradle)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Array, Table

from conduit.detect.pip_manifests import iter_pip_manifests
from conduit.prune.grep_imports import SKIP_DIRS

Op = Literal["bump", "add", "remove"]

DEP_RULE_TYPES = frozenset(
    {"DEPENDENCY_BUMP", "DEPENDENCY_ADD", "DEPENDENCY_REMOVE"}
)
_POETRY_SKIP = {"python", "python-versions"}
_PYPI_JSON_TIMEOUT_S = 20.0
_POETRY_LOCK_TIMEOUT_S = 600.0


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
    """Match package pin on one line (legacy helper for simple lookups)."""
    return re.compile(
        rf"(?m)^(?P<lead>\s*){re.escape(package)}\s*(?:==|>=|~=|<=|>|<)?\s*[^\s#]*"
    )


def _req_entry_pattern(package: str) -> re.Pattern[str]:
    """Match a requirements entry including markers and ``--hash=`` continuations."""
    return re.compile(
        rf"(?ms)^(?P<lead>\s*){re.escape(package)}"
        rf"(?P<body>\s*(?:==|>=|~=|<=|>|<)\s*[^\s#\\]+)?"
        rf"(?P<rest>[^\n]*)"
        rf"(?P<hashes>(?:\n[ \t]+--hash=[^\n]*)*)"
    )


def _clean_req_rest(rest: str) -> str:
    """Drop inline hashes and a trailing ``\\`` used only for hash continuations."""
    text = re.sub(r"(?i)\s*--hash=\S+", "", rest or "")
    text = text.rstrip()
    if text.endswith("\\"):
        text = text[:-1].rstrip()
    return text


def _requirements_file_uses_hashes(text: str) -> bool:
    return bool(re.search(r"(?i)--hash=", text or ""))


def _normalize_pip_version(version: str) -> str:
    v = (version or "").strip()
    if v.startswith(("==", ">=", "<=", "~=", ">", "<")):
        v = re.split(r"[=<>!~]+", v, maxsplit=1)[-1].strip()
    if v.startswith("v") and len(v) > 1 and v[1].isdigit():
        v = v[1:]
    return v


def _pypi_get_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=_PYPI_JSON_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def fetch_pypi_version_json(package: str, version: str) -> dict[str, Any] | None:
    pkg = (package or "").strip()
    ver = _normalize_pip_version(version)
    if not pkg or not ver:
        return None
    return _pypi_get_json(f"https://pypi.org/pypi/{pkg}/{ver}/json")


def fetch_pypi_project_json(package: str) -> dict[str, Any] | None:
    pkg = (package or "").strip()
    if not pkg:
        return None
    return _pypi_get_json(f"https://pypi.org/pypi/{pkg}/json")


def fetch_pypi_sha256s(package: str, version: str) -> list[str]:
    """Return distinct sha256 digests for ``package==version`` artifacts on PyPI."""
    payload = fetch_pypi_version_json(package, version)
    if not payload:
        return []
    urls = payload.get("urls")
    if not isinstance(urls, list):
        return []
    digests: list[str] = []
    seen: set[str] = set()
    for item in urls:
        if not isinstance(item, dict):
            continue
        digest = (item.get("digests") or {}).get("sha256")
        if not isinstance(digest, str):
            continue
        digest = digest.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            continue
        if digest in seen:
            continue
        seen.add(digest)
        digests.append(digest)
    return digests


def fetch_pypi_requires_dist(package: str, version: str) -> list[str]:
    """Return ``requires_dist`` entries for a package version on PyPI."""
    payload = fetch_pypi_version_json(package, version)
    if not payload:
        return []
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    raw = info.get("requires_dist") or []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x).strip()]


def pick_pypi_version(package: str, specifier: str = "") -> str | None:
    """Pick the newest non-prerelease PyPI version matching ``specifier``."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    payload = fetch_pypi_project_json(package)
    if not payload:
        return None
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        return None
    try:
        spec = SpecifierSet(specifier or "")
    except InvalidSpecifier:
        return None
    matched: list[Version] = []
    matched_pre: list[Version] = []
    for raw in releases:
        try:
            ver = Version(str(raw))
        except InvalidVersion:
            continue
        if ver not in spec:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            matched_pre.append(ver)
        else:
            matched.append(ver)
    chosen = matched or matched_pre
    if not chosen:
        return None
    return str(sorted(chosen)[-1])


def _pinned_requirement_names(text: str) -> set[str]:
    """Canonical names already present as top-level pins in a requirements file."""
    from packaging.utils import canonicalize_name

    names: set[str] = set()
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("--"):
            continue
        s = s.split("\\", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*(?:==|>=|~=|<=|>|<)", s)
        if not m:
            continue
        names.add(canonicalize_name(m.group(1)))
    return names


def ensure_hashed_transitive_pins(
    text: str,
    package: str,
    version: str,
    *,
    max_new: int = 64,
) -> str:
    """
    Append missing transitive ``pkg==ver`` + ``--hash=`` lines for require-hashes.

    Walks PyPI ``requires_dist`` from ``package==version`` (BFS). Skips deps already
    pinned in ``text`` and markers that fail in the current environment.
    """
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name

    root_pkg = (package or "").strip()
    root_ver = _normalize_pip_version(version)
    if not root_pkg or not root_ver or not _requirements_file_uses_hashes(text):
        return text

    pinned = _pinned_requirement_names(text)
    pinned.add(canonicalize_name(root_pkg))
    queue: list[tuple[str, str]] = [(root_pkg, root_ver)]
    seen_nodes: set[tuple[str, str]] = {(canonicalize_name(root_pkg), root_ver)}
    blocks: list[str] = []

    while queue and len(blocks) < max_new:
        pkg, ver = queue.pop(0)
        for raw in fetch_pypi_requires_dist(pkg, ver):
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if req.marker is not None:
                try:
                    if not req.marker.evaluate():
                        continue
                except Exception:
                    continue
            name = canonicalize_name(req.name)
            if name in pinned:
                continue
            chosen = pick_pypi_version(req.name, str(req.specifier))
            if not chosen:
                continue
            digests = fetch_pypi_sha256s(req.name, chosen)
            if not digests:
                continue
            spec = format_pip_requirement(req.name, chosen)
            blocks.append(_format_req_with_hashes("", spec, "", digests))
            pinned.add(name)
            node = (name, chosen)
            if node not in seen_nodes:
                seen_nodes.add(node)
                queue.append((req.name, chosen))
            if len(blocks) >= max_new:
                break

    if not blocks:
        return text
    body = text if text.endswith("\n") or not text else text + "\n"
    # Keep a blank line before Conduit-added transitive pins for readability.
    if body and not body.endswith("\n\n"):
        body = body.rstrip("\n") + "\n\n"
    return body + "\n\n".join(blocks) + "\n"


def _format_req_with_hashes(lead: str, spec: str, rest: str, hashes: list[str]) -> str:
    """Render ``spec`` + markers + ``--hash=`` continuations (poetry/pip-tools style)."""
    markers = _clean_req_rest(rest)
    if not hashes:
        return f"{lead}{spec}{markers}"
    lines = [f"{lead}{spec}{markers} \\"]
    for i, digest in enumerate(hashes):
        suffix = " \\" if i < len(hashes) - 1 else ""
        lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines)


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
    entry = _req_entry_pattern(package)
    file_hashed = _requirements_file_uses_hashes(original)
    if op == "remove":
        updated, n = entry.subn("", original)
        if n == 0:
            return False
        updated = re.sub(r"\n{3,}", "\n\n", updated)
    elif entry.search(original):
        spec = format_pip_requirement(package, version)
        # In --require-hashes mode (any --hash= in the file), the bumped pin must
        # carry digests that match the wheels pip will download.
        need_hashes = file_hashed
        digests = fetch_pypi_sha256s(package, version) if need_hashes else []

        def _repl(match: re.Match[str]) -> str:
            rest = match.group("rest") or ""
            if digests:
                return _format_req_with_hashes(
                    match.group("lead"), spec, rest, digests
                )
            # No digests available: strip stale hashes so we don't keep wrong ones.
            return f"{match.group('lead')}{spec}{_clean_req_rest(rest)}"

        updated, n = entry.subn(_repl, original)
        if n == 0:
            return False
        if need_hashes:
            updated = ensure_hashed_transitive_pins(updated, package, version)
    elif op == "add":
        spec = format_pip_requirement(package, version)
        if file_hashed:
            digests = fetch_pypi_sha256s(package, version)
            block = _format_req_with_hashes("", spec, "", digests) if digests else spec
        else:
            block = spec
        sep = "" if original.endswith("\n") or not original else "\n"
        updated = f"{original}{sep}{block}\n"
        if file_hashed:
            updated = ensure_hashed_transitive_pins(updated, package, version)
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
    return iter_pip_manifests(root, scope=scope)


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


def is_poetry_project(root: Path) -> bool:
    """True when ``pyproject.toml`` declares ``[tool.poetry]``."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"(?m)^\[tool\.poetry\]", text))


def hashed_requirements_files(root: Path, *, scope: str = "main") -> list[Path]:
    """Requirements manifests under ``root`` that use ``--hash=`` pins."""
    out: list[Path] = []
    for path in _iter_pip_manifests(root, scope=scope):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _requirements_file_uses_hashes(text):
            out.append(path)
    return out


def _run_poetry(
    root: Path,
    args: list[str],
    *,
    timeout: float = _POETRY_LOCK_TIMEOUT_S,
) -> tuple[bool, str]:
    exe = shutil.which("poetry")
    if not exe:
        return False, "poetry CLI not found on PATH"
    try:
        proc = subprocess.run(
            [exe, *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        blob = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        return False, blob or f"poetry {' '.join(args)} exit {proc.returncode}"
    return True, ""


def refresh_poetry_hashed_requirements(
    root: Path,
    *,
    output: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Re-resolve ``poetry.lock`` and export a hashed ``requirements.txt``.

    Needed after bumping a direct dep in a Poetry project: ``--require-hashes``
    installs fail if new transitive pins (e.g. httpx2) are missing from the
    export. Returns ``(ok, detail)``.
    """
    root = root.resolve()
    out = (output or (root / "requirements.txt")).resolve()
    if dry_run:
        return True, f"would run poetry lock + export -> {out.name}"
    if not is_poetry_project(root):
        return False, "not a Poetry project"
    ok, detail = _run_poetry(root, ["lock", "--no-interaction"])
    if not ok:
        return False, detail
    # Prefer writing hashes (default). ``--without-hashes`` would break
    # consumers that already use --require-hashes mode.
    ok, detail = _run_poetry(
        root,
        [
            "export",
            "-f",
            "requirements.txt",
            "--output",
            str(out),
            "--no-interaction",
        ],
    )
    if not ok:
        # poetry-plugin-export may be missing on Poetry 2.x
        return False, detail
    if not out.is_file():
        return False, f"poetry export did not create {out}"
    return True, ""


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

    # Prefer pyproject (Poetry source of truth) before requirements.txt.
    pyproject_changed = False
    if "pyproject" in ecosystems:
        pyproject = root / "pyproject.toml"
        ok, skip = _edit_pyproject(
            pyproject, package, version, op=op, scope=scope, dry_run=dry_run
        )
        if ok:
            result.changed.append(_rel(pyproject))
            pyproject_changed = True
        elif skip:
            result.skips.append(skip)

    poetry_exported = False
    hashed_reqs = (
        hashed_requirements_files(root, scope=scope)
        if ("pip" in ecosystems or "pyproject" in ecosystems)
        else []
    )
    if (
        is_poetry_project(root)
        and hashed_reqs
        and ("pip" in ecosystems or "pyproject" in ecosystems)
    ):
        # Ensure Poetry table is bumped even when the rule only listed pip.
        if not pyproject_changed and not dry_run:
            ok, _skip = _edit_pyproject(
                root / "pyproject.toml",
                package,
                version,
                op=op,
                scope=scope,
                dry_run=False,
            )
            if ok:
                result.changed.append("pyproject.toml")
                pyproject_changed = True

        primary = root / "requirements.txt"
        hashed_resolved = {p.resolve() for p in hashed_reqs}
        if primary.resolve() in hashed_resolved:
            export_target = primary
        else:
            export_target = hashed_reqs[0]
        ok, detail = refresh_poetry_hashed_requirements(
            root, output=export_target, dry_run=dry_run
        )
        if ok:
            poetry_exported = True
            rel_out = _rel(export_target)
            if rel_out not in result.changed:
                result.changed.append(rel_out)
            lock = root / "poetry.lock"
            if lock.is_file() or dry_run:
                rel_lock = "poetry.lock"
                if rel_lock not in result.changed:
                    result.changed.append(rel_lock)
        else:
            result.skips.append(
                f"poetry lock/export failed ({detail}); "
                "falling back to direct requirements.txt edit"
            )

    if "pip" in ecosystems and not poetry_exported:
        pip_paths = _iter_pip_manifests(root, scope=scope)
        if scope == "dev" and not pip_paths:
            result.skips.append(
                "Skipped requirements-dev.txt: file does not exist"
            )
        for req in pip_paths:
            if _edit_requirements_txt(req, package, version, op=op, dry_run=dry_run):
                result.changed.append(_rel(req))

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
