"""Apply Migration Packet rules to a target repository."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from conduit.context_filter import file_has_vendor_context
from conduit.patcher.ast_attr_call import apply_attr_rename, apply_call_rewrite
from conduit.patcher.ast_import_rewrite import apply_import_rewrite
from conduit.patcher.ast_param_rename import apply_param_rename
from conduit.patcher.dependency_update import apply_dependency_bump
from conduit.patcher.key_rename import apply_key_rename, is_env_file, iter_config_files
from conduit.patcher.string_replace import exact_replace, regex_replace, write_if_changed
from conduit.prune.grep_imports import SKIP_DIRS

SCAN_SUFFIXES = {
    ".py",
    ".ts",
    ".js",
    ".tsx",
    ".jsx",
    ".java",
    ".go",
    ".yaml",
    ".yml",
    ".json",
}


@dataclass
class ChangeRecord:
    event_id: str
    path: str
    rule_type: str
    detail: str


@dataclass
class PatchReport:
    changes: list[ChangeRecord] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)

    def add(self, record: ChangeRecord) -> None:
        self.changes.append(record)
        if record.path not in self.files_modified:
            self.files_modified.append(record.path)


def _iter_candidate_files(
    root: Path, allowlist: Iterable[Path] | None = None
) -> list[Path]:
    if allowlist is not None:
        return [p.resolve() for p in allowlist if p.is_file()]

    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith(".env") or path.suffix.lower() in SCAN_SUFFIXES:
            out.append(path)
        elif path.name in {
            "requirements.txt",
            "pyproject.toml",
            "package.json",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
        }:
            out.append(path)
    return out


def _glob_ok(path: Path, patterns: list[str], root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    name = path.name
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def _net_dependency_bumps(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the last DEPENDENCY_BUMP per package (opposing bumps cancel)."""
    last_idx: dict[str, int] = {}
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict) or rule.get("type") != "DEPENDENCY_BUMP":
            continue
        key = str(rule.get("package") or "").lower() or "_"
        last_idx[key] = i
    if not last_idx:
        return rules
    keep = set(last_idx.values())
    out: list[dict[str, Any]] = []
    for i, rule in enumerate(rules):
        if (
            isinstance(rule, dict)
            and rule.get("type") == "DEPENDENCY_BUMP"
            and i not in keep
        ):
            continue
        out.append(rule)
    return out


def apply_packet(
    root: Path,
    packet: dict[str, Any],
    *,
    dry_run: bool = False,
    require_context: bool = True,
    file_allowlist: Iterable[Path] | None = None,
) -> PatchReport:
    """Apply a single conduit-packet.json."""
    root = root.resolve()
    report = PatchReport()
    files = _iter_candidate_files(root, file_allowlist)
    for name in (
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    ):
        path = root / name
        if path.is_file() and path.resolve() not in files:
            files.append(path.resolve())

    if any(
        isinstance(rule, dict) and rule.get("type") == "KEY_RENAME"
        for rule in packet.get("rules") or []
    ):
        seen = {p.resolve() for p in files}
        for path in iter_config_files(root):
            if path not in seen:
                files.append(path)
                seen.add(path)

    packet_id = packet.get("packet_id", "packet")
    vendor = packet.get("package", "")
    rules = _net_dependency_bumps(list(packet.get("rules") or []))

    for rule in rules:
        rule_type = rule.get("type")
        if rule_type == "DEPENDENCY_BUMP":
            for rel in apply_dependency_bump(root, rule, dry_run=dry_run):
                report.add(
                    ChangeRecord(
                        event_id=packet_id,
                        path=rel,
                        rule_type=rule_type,
                        detail=(
                            f"Bumped {rule.get('package')} "
                            f"{rule.get('from_version')} -> {rule.get('to_version')}"
                        ),
                    )
                )
            continue

        target_files = rule.get("target_files") or ["*"]
        for path in files:
            if not _glob_ok(path, target_files, root):
                continue
            try:
                original = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if require_context and not file_has_vendor_context(path, original, vendor):
                continue

            updated = original
            detail = ""
            count = 0

            if rule_type == "EXACT_STRING_REPLACE":
                updated, count = exact_replace(
                    original, rule.get("match", ""), rule.get("replace", "")
                )
                detail = f'Replaced "{rule.get("match")}" -> "{rule.get("replace")}" ({count}x)'
            elif rule_type == "REGEX_REPLACE":
                updated, count = regex_replace(
                    original, rule.get("pattern", ""), rule.get("replace", "")
                )
                detail = f"Regex replace ({count}x)"
            elif rule_type == "AST_PARAM_RENAME":
                updated, count = apply_param_rename(
                    path,
                    original,
                    function_target=rule.get("function_target", ""),
                    old_param=rule.get("old_param", ""),
                    new_param=rule.get("new_param", ""),
                )
                detail = (
                    f"Renamed param {rule.get('old_param')} -> "
                    f"{rule.get('new_param')} ({count}x)"
                )
            elif rule_type == "AST_IMPORT_REWRITE":
                updated, count = apply_import_rewrite(
                    path,
                    original,
                    old_import=rule.get("old_import", ""),
                    new_import=rule.get("new_import", ""),
                )
                detail = (
                    f'Rewrote import "{rule.get("old_import")}" -> '
                    f'"{rule.get("new_import")}" ({count}x)'
                )
            elif rule_type == "AST_ATTR_RENAME":
                updated, count = apply_attr_rename(
                    path,
                    original,
                    old_attr=rule.get("old_attr", ""),
                    new_attr=rule.get("new_attr", ""),
                )
                detail = (
                    f'Renamed attr "{rule.get("old_attr")}" -> '
                    f'"{rule.get("new_attr")}" ({count}x)'
                )
            elif rule_type == "AST_CALL_REWRITE":
                updated, count = apply_call_rewrite(
                    path,
                    original,
                    old_callee=rule.get("old_callee", ""),
                    new_callee=rule.get("new_callee", ""),
                )
                detail = (
                    f'Rewrote call "{rule.get("old_callee")}" -> '
                    f'"{rule.get("new_callee")}" ({count}x)'
                )
            elif rule_type == "KEY_RENAME":
                updated, count = apply_key_rename(
                    original,
                    str(rule.get("old_key") or ""),
                    str(rule.get("new_key") or ""),
                    env_file=is_env_file(path),
                )
                detail = (
                    f'Renamed key "{rule.get("old_key")}" -> '
                    f'"{rule.get("new_key")}" ({count}x)'
                )
            else:
                continue

            if count and write_if_changed(path, original, updated, dry_run=dry_run):
                rel = str(path.relative_to(root))
                report.add(
                    ChangeRecord(
                        event_id=packet_id,
                        path=rel,
                        rule_type=rule_type or "UNKNOWN",
                        detail=detail,
                    )
                )
    return report


def apply_events(
    root: Path,
    events: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    require_context: bool = True,
    file_allowlist: Iterable[Path] | None = None,
) -> PatchReport:
    """Backward-compatible: treat registry-style events as mini-packets."""
    root = root.resolve()
    report = PatchReport()
    for event in events:
        packet = {
            "packet_id": event.get("event_id", "event"),
            "package": event.get("vendor", ""),
            "ecosystem": "other",
            "from_version": "0",
            "to_version": "1",
            "rules": event.get("rules") or [],
        }
        sub = apply_packet(
            root,
            packet,
            dry_run=dry_run,
            require_context=require_context,
            file_allowlist=file_allowlist,
        )
        for change in sub.changes:
            report.add(change)
    return report
