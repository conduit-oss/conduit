"""Post-green surface sync: migrate leftover tokens in docs/ops after verify."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from conduit.patcher.string_replace import exact_replace
from conduit.prune.grep_imports import SKIP_DIRS
from conduit.surface_paths import is_prose_ops_rel
from conduit.text_tokens import token_in_text

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class SurfaceSyncReport:
    files_modified: list[str] = field(default_factory=list)
    replacements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_modified": list(self.files_modified),
            "replacements": list(self.replacements),
        }


def _replacement_map(packet: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered (old, new) pairs from packet string rules + forbidden tokens."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "EXACT_STRING_REPLACE":
            continue
        old = str(rule.get("match") or "").strip()
        new = str(rule.get("replace") or "").strip()
        if not old or old in seen:
            continue
        seen.add(old)
        pairs.append((old, new))
    # Longer tokens first to avoid partial stomps
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def discover_surface_paths(root: Path) -> list[str]:
    """Find prose/ops paths under the consumer repo."""
    root = root.resolve()
    out: list[str] = []
    for name in ("README.md", "README.rst", "README.txt", "SCENARIOS.md", "CHANGELOG.md"):
        path = root / name
        if path.is_file():
            out.append(name)
    for folder_name in ("docs", "scripts"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if is_prose_ops_rel(rel):
                out.append(rel)
    for name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml"):
        path = root / name
        if path.is_file() and name not in out:
            out.append(name)
    # dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for rel in out:
        if rel not in seen:
            seen.add(rel)
            uniq.append(rel)
    return uniq


def sync_surfaces(
    root: Path,
    packet: dict[str, Any],
    *,
    extra_paths: list[str] | None = None,
    log: LogFn | None = None,
    dry_run: bool = False,
) -> SurfaceSyncReport:
    """Apply packet string replacements on prose/ops surfaces."""
    emit = log or _noop
    root = root.resolve()
    report = SurfaceSyncReport()
    pairs = _replacement_map(packet)
    if not pairs:
        emit("[surface-sync] no EXACT_STRING_REPLACE pairs; skip")
        return report

    paths = discover_surface_paths(root)
    for rel in extra_paths or []:
        rel = rel.replace("\\", "/")
        if rel not in paths and is_prose_ops_rel(rel):
            paths.append(rel)

    for rel in paths:
        path = root / rel
        if not path.is_file():
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        updated = original
        local_hits: list[str] = []
        for old, new in pairs:
            if not token_in_text(updated, old):
                continue
            updated, count = exact_replace(updated, old, new)
            if count:
                local_hits.append(f"{old!r} → {new!r} ({count}x)")
        if updated == original:
            continue
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
        report.files_modified.append(rel)
        report.replacements.extend(f"{rel}: {h}" for h in local_hits)
        emit(f"[surface-sync] {rel}: " + "; ".join(local_hits[:4]))

    if report.files_modified:
        emit(f"[surface-sync] updated {len(report.files_modified)} surface file(s)")
    else:
        emit("[surface-sync] no leftover tokens in prose/ops surfaces")
    return report
