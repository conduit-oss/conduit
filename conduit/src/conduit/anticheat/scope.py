"""Audit scope: which paths the LLM anti-cheat may judge."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Iterable

from conduit.anticheat.findings import AnticheatFinding
from conduit.anticheat.rules import imports_package, is_impl_rel, packet_package
from conduit.anticheat.vendor import audit_exclude_globs
from conduit.prune.grep_imports import SKIP_DIRS
from conduit.repair_ignore import IgnoreList, build_ignore_list

_SCAN_SUFFIXES = {".py", ".ts", ".js", ".tsx", ".jsx", ".sh"}


def _posix(rel: str) -> str:
    return rel.replace("\\", "/")


def _glob_ok(rel: str, patterns: list[str]) -> bool:
    name = Path(rel).name
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
            return True
    return False


def is_migration_impl_rel(rel: str, text: str, packet: dict[str, Any]) -> bool:
    """Impl file that imports the migrated package."""
    if not is_impl_rel(rel):
        return False
    pkg = packet_package(packet)
    if pkg and imports_package(text, pkg):
        return True
    for item in packet.get("import_files") or []:
        if isinstance(item, str) and _posix(item) == _posix(rel):
            return True
    return False


def iter_scannable_rels(root: Path, files: Iterable[str] | None = None) -> list[str]:
    root = root.resolve()
    if files:
        return [_posix(r) for r in files]
    rels: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _SCAN_SUFFIXES:
            continue
        try:
            rels.append(path.relative_to(root).as_posix())
        except ValueError:
            continue
    return rels


def impl_paths(root: Path, packet: dict[str, Any]) -> set[str]:
    """Repo paths that import/reference the migrated package."""
    root = root.resolve()
    out: set[str] = set()
    for rel in iter_scannable_rels(root):
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if is_migration_impl_rel(rel, text, packet):
            out.add(rel)
    for item in packet.get("import_files") or []:
        if isinstance(item, str):
            rel = _posix(item)
            if (root / rel).is_file():
                out.add(rel)
    return out


def audit_scope_paths(
    root: Path,
    packet: dict[str, Any],
    *,
    mechanical_findings: list[AnticheatFinding] | None = None,
    block_findings: list[AnticheatFinding] | None = None,
    ignore: IgnoreList | None = None,
) -> set[str]:
    """Paths the LLM auditor may read/judge."""
    root = root.resolve()
    ignore = ignore or build_ignore_list(root, packet)
    scope = set(impl_paths(root, packet))

    for finding in block_findings or mechanical_findings or []:
        if finding.path:
            scope.add(_posix(finding.path))

    exclude = audit_exclude_globs(packet)
    pkt_anticheat = packet.get("anticheat") or {}
    if isinstance(pkt_anticheat, dict):
        for g in pkt_anticheat.get("audit_exclude_globs") or []:
            if g:
                exclude.append(str(g))

    filtered: set[str] = set()
    for rel in scope:
        if ignore.path_ignored(rel):
            continue
        if exclude and _glob_ok(rel, exclude):
            # Keep if explicitly cited by a block finding
            cited = any(
                _posix(f.path) == rel and f.severity == "block"
                for f in (block_findings or mechanical_findings or [])
            )
            if not cited:
                continue
        filtered.add(rel)
    return filtered
