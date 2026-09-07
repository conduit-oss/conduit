"""Fast import-string pre-filter before AST/patch work."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
    ".conduit",
}

SCAN_SUFFIXES = {".py", ".ts", ".js", ".tsx", ".jsx", ".java", ".go"}
_CONFIG_EXPAND_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini"}
_EXPAND_SKIP_PARTS = {
    "migrations",
    "static",
    "plugins",
    "media",
    "docs",
    "node_modules",
    "vendor",
}


def expand_allowlist_for_exact_rules(
    root: Path,
    files: list[Path],
    packet: dict,
) -> list[Path]:
    """Include config files that still contain EXACT_STRING_REPLACE matches.

    Extra files are configs only (``.env*``, yaml/json/toml/ini) — not every
    ``.py`` that mentions a model id. Importers stay the apply core.
    """
    matches: list[str] = []
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") != "EXACT_STRING_REPLACE":
            continue
        match = rule.get("match")
        if isinstance(match, str) and match:
            matches.append(match)
    if not matches:
        return files

    seen = {p.resolve() for p in files}
    out = list(files)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part in _EXPAND_SKIP_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in _CONFIG_EXPAND_SUFFIXES:
            if not path.name.startswith(".env"):
                continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(m in text for m in matches):
            out.append(resolved)
            seen.add(resolved)
    return out


def expand_apply_allowlist_oracle(
    root: Path,
    files: list[Path],
    packet: dict,
    *,
    changed_files: list[str] | None = None,
) -> list[Path]:
    """Add oracle scan paths so REST rules reach config/script files."""
    from conduit.test_gen import oracle_scan_rels

    seen = {p.resolve() for p in files}
    out = list(files)
    for rel in oracle_scan_rels(
        root,
        packet,
        changed_files=changed_files,
        file_allowlist=files,
    ):
        path = (root / rel).resolve()
        if path.is_file() and path not in seen:
            out.append(path)
            seen.add(path)
    return out


def _import_patterns(package: str) -> list[re.Pattern[str]]:
    pkg = re.escape(package)
    return [
        re.compile(rf"\bfrom\s+{pkg}\b"),
        re.compile(rf"\bimport\s+{pkg}\b"),
        re.compile(rf"""from\s+['"]{pkg}['"]"""),
        re.compile(rf"""import\s*\(\s*['"]{pkg}['"]"""),
        re.compile(rf"""require\s*\(\s*['"]{pkg}['"]\s*\)"""),
        re.compile(rf"""from\s+['"]{pkg}/"""),
        # Java: import com.foo.Bar; / import static ...
        re.compile(rf"\bimport\s+(?:static\s+)?{pkg}(?:\.|;|\s)"),
        # Go: import "module/path" or import ( "module/path" )
        re.compile(rf"""["`]{pkg}["`]"""),
        re.compile(rf"""["`]{pkg}/"""),
    ]


def iter_source_files(root: Path) -> Iterable[Path]:
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SCAN_SUFFIXES:
            yield path


def prune_by_imports(root: Path, packages: Iterable[str]) -> list[Path]:
    """Return files that appear to import any of the given packages."""
    pkgs = [p for p in packages if p]
    if not pkgs:
        return list(iter_source_files(root))

    patterns = {pkg: _import_patterns(pkg) for pkg in pkgs}
    hits: list[Path] = []
    for path in iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pkg, pats in patterns.items():
            if any(p.search(text) for p in pats):
                hits.append(path)
                break
            # soft: package name appears near import-like tokens
            if pkg in text and ("import" in text or "require" in text):
                hits.append(path)
                break
    return hits
