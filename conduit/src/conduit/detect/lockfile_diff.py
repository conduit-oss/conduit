"""Detect dependency version jumps from git diffs on manifests/lockfiles."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from conduit.detect.models import VersionJump

MANIFEST_NAMES = {
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "poetry.lock",
    "Pipfile.lock",
    "go.mod",
}


def _git(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        # Windows raises NotADirectoryError (WinError 267) when cwd is missing
        # or not a directory; treat like a failed git invocation.
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _parse_requirements_lines(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([=<>!~]+)\s*([^\s;#]+)", line)
        if m:
            out[m.group(1).lower()] = m.group(3).strip()
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)", line)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
    return out


def _parse_package_json_deps(text: str) -> dict[str, str]:
    return {name: ver for name, (ver, _scope) in _parse_package_json_scoped(text).items()}


def _parse_package_json_scoped(text: str) -> dict[str, tuple[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, tuple[str, str]] = {}
    sections = (
        ("dependencies", "main"),
        ("devDependencies", "dev"),
        ("peerDependencies", "peer"),
    )
    for key, scope in sections:
        block = data.get(key) or {}
        if isinstance(block, dict):
            for name, ver in block.items():
                out[str(name).lower()] = (str(ver).lstrip("^~>=< "), scope)
    return out


def _parse_pyproject_deps(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    # Minimal TOML-ish parse for poetry/pep621 dependency strings
    for m in re.finditer(
        r'["\']([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?[=<>!~]+([^"\']+)["\']',
        text,
    ):
        out[m.group(1).lower()] = m.group(2).strip()
    for m in re.finditer(
        r'^([A-Za-z0-9_.\-]+)\s*=\s*["\']([^"\']+)["\']',
        text,
        re.MULTILINE,
    ):
        name = m.group(1).lower()
        if name in {"name", "version", "description", "readme", "requires-python"}:
            continue
        ver = m.group(2).strip()
        if re.match(r"^[\d*]", ver) or ver.startswith("^") or ver.startswith("~"):
            out[name] = ver.lstrip("^~>=< ")
    return out


def _parse_go_mod(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"^\s*([^\s]+)\s+v([0-9][^\s]+)", text, re.MULTILINE):
        out[m.group(1).lower()] = m.group(2)
    return out


def _parse_file(name: str, text: str) -> tuple[str, dict[str, str]]:
    lower = name.lower()
    if lower.endswith("requirements.txt") or "requirements" in lower and lower.endswith(".txt"):
        return "pypi", _parse_requirements_lines(text)
    if lower == "package.json" or lower == "package-lock.json":
        return "npm", _parse_package_json_deps(text)
    if lower == "pyproject.toml" or lower == "poetry.lock":
        return "pypi", _parse_pyproject_deps(text)
    if lower == "go.mod":
        return "go", _parse_go_mod(text)
    if lower.endswith(".txt"):
        return "pypi", _parse_requirements_lines(text)
    return "unknown", {}


def _parse_file_scoped(
    name: str, text: str
) -> tuple[str, dict[str, tuple[str, str]]]:
    """ecosystem + package -> (version, scope)."""
    lower = name.lower()
    default_scope = "dev" if "dev" in lower and lower.endswith(".txt") else "main"
    eco, deps = _parse_file(name, text)
    if lower == "package.json" or lower == "package-lock.json":
        return eco, _parse_package_json_scoped(text)
    return eco, {k: (v, default_scope) for k, v in deps.items()}


def diff_versions(
    old_text: str,
    new_text: str,
    *,
    filename: str,
) -> list[VersionJump]:
    eco_old, old_deps = _parse_file_scoped(filename, old_text)
    eco_new, new_deps = _parse_file_scoped(filename, new_text)
    ecosystem = eco_new if eco_new != "unknown" else eco_old
    jumps: list[VersionJump] = []
    for name, (new_ver, new_scope) in new_deps.items():
        if name not in old_deps:
            jumps.append(
                VersionJump(
                    name=name,
                    from_version="",
                    to_version=new_ver,
                    ecosystem=ecosystem,
                    manifest=filename,
                    kind="add",
                    scope=new_scope,
                )
            )
            continue
        old_ver, old_scope = old_deps[name]
        if old_ver != new_ver:
            jumps.append(
                VersionJump(
                    name=name,
                    from_version=old_ver,
                    to_version=new_ver,
                    ecosystem=ecosystem,
                    manifest=filename,
                    kind="bump",
                    scope=new_scope or old_scope,
                )
            )
    for name, (old_ver, old_scope) in old_deps.items():
        if name not in new_deps:
            jumps.append(
                VersionJump(
                    name=name,
                    from_version=old_ver,
                    to_version="",
                    ecosystem=ecosystem,
                    manifest=filename,
                    kind="remove",
                    scope=old_scope,
                )
            )
    return jumps


def detect_lockfile_jumps(
    root: Path,
    *,
    base_ref: str | None = None,
    majors_only: bool = True,
) -> list[VersionJump]:
    """
    Compare manifest/lockfile contents against base_ref (or HEAD~1).
    If no git history, returns empty list.
    """
    root = root.resolve()
    if not root.is_dir():
        return []
    # .git may be absent in linked work trees; ask git directly.
    if _git(root, "rev-parse", "--is-inside-work-tree").strip() != "true":
        return []

    ref = base_ref or "HEAD~1"
    # Prefer merge-base with origin/main when base_ref looks like a branch tip
    changed = _git(root, "diff", "--name-only", f"{ref}...HEAD")
    if not changed.strip() and base_ref is None:
        changed = _git(root, "diff", "--name-only", "HEAD")
        # unstaged + staged vs HEAD for working tree
        unstaged = _git(root, "diff", "--name-only")
        staged = _git(root, "diff", "--name-only", "--cached")
        changed = "\n".join(filter(None, [changed, unstaged, staged]))

    jumps: list[VersionJump] = []
    seen: set[tuple[str, str, str]] = set()
    for rel in changed.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        name = Path(rel).name
        if name not in MANIFEST_NAMES and not name.startswith("requirements"):
            continue
        old_text = _git(root, "show", f"{ref}:{rel}")
        new_path = root / rel
        if not new_path.is_file():
            continue
        try:
            new_text = new_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not old_text:
            # new file — skip version jump detection
            continue
        for jump in diff_versions(old_text, new_text, filename=name):
            key = (jump.name, jump.from_version, jump.to_version)
            if key in seen:
                continue
            if jump.kind == "bump" and majors_only and not jump.is_major:
                continue
            seen.add(key)
            jumps.append(jump)
    return jumps
