"""Consumer AST calls on an imported package, matched against export-delta removals."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from conduit.export_delta.resources import resource_path_for
from conduit.prune.grep_imports import SKIP_DIRS


@dataclass(frozen=True)
class PackageCall:
    rel: str
    callee: str
    lineno: int = 0


def collect_package_calls(
    root: Path,
    files: Iterable[Path],
    package: str,
) -> list[PackageCall]:
    """AST Call chains on ``package`` / names imported from it (impl files only)."""
    root = root.resolve()
    pkg = (package or "").strip()
    if not pkg:
        return []
    out: list[PackageCall] = []
    seen: set[tuple[str, str, int]] = set()
    for path in files:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved.suffix.lower() != ".py":
            continue
        if any(part in SKIP_DIRS for part in resolved.parts):
            continue
        try:
            rel = str(resolved.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        if rel.startswith("tests/") or "/tests/" in rel:
            continue
        try:
            text = resolved.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        for callee, lineno in _calls_in_source(text, pkg):
            key = (rel, callee, lineno)
            if key in seen:
                continue
            seen.add(key)
            out.append(PackageCall(rel=rel, callee=callee, lineno=lineno))
    return out


def call_hits_removed(callee: str, removed: Iterable[str]) -> bool:
    """True when a consumer callee uses a from-pin symbol that the to-pin dropped."""
    token = (callee or "").strip()
    if not token:
        return False
    gone = {str(s).strip() for s in removed if str(s).strip()}
    if not gone:
        return False
    forms = _callee_forms(token)
    if forms & gone:
        return True
    for item in gone:
        if "." not in item and any(
            f == item or f.startswith(item + ".") or f.endswith("." + item)
            or f".{item}." in f
            for f in forms
        ):
            return True
    return False


def leftover_calls(
    calls: Iterable[PackageCall],
    removed: Iterable[str],
) -> list[PackageCall]:
    return [c for c in calls if call_hits_removed(c.callee, removed)]


def legacy_resource_calls(
    calls: Iterable[PackageCall],
    resource_paths: dict[str, str] | None,
) -> list[PackageCall]:
    """Consumer calls that still map to a from-pin REST path (0.x resource methods)."""
    table = resource_paths or {}
    if not table:
        return []
    return [c for c in calls if resource_path_for(c.callee, table)]


def merge_calls_into_api_patterns(
    api_patterns: list[str],
    calls: Iterable[PackageCall],
) -> list[str]:
    out = list(api_patterns)
    seen = {p.strip() for p in out}
    for call in calls:
        token = call.callee.strip()
        if not token or token in seen:
            continue
        out.append(token)
        seen.add(token)
    return out


def _callee_forms(token: str) -> set[str]:
    raw = token.strip()
    forms = {raw}
    if raw.startswith("openai."):
        forms.add(raw[7:])
    else:
        forms.add(f"openai.{raw}")
    return forms


def _calls_in_source(source: str, package: str) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(package + "."):
                    imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == package:
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported.add(alias.asname or alias.name)
    if package not in imported:
        imported.add(package)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _attr_chain(node.func)
        if not chain:
            continue
        root_name = chain.split(".", 1)[0]
        if root_name == package or root_name in imported:
            out.append((chain, getattr(node, "lineno", 0) or 0))
            continue
        # Bare ChatCompletion.create after `from openai import ChatCompletion`
        if chain in imported or chain.split(".", 1)[0] in imported:
            if root_name[:1].isupper() or "." in chain:
                out.append((chain, getattr(node, "lineno", 0) or 0))
    return out


def _attr_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None
