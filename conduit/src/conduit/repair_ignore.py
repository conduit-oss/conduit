"""Dynamic ignore lists for self-correct heuristics and LLM repair/synth.

Sources (merged):
1. Packet ``ignore`` object (globs / paths / patterns)
2. ``.conduit/ignore.json`` in the consumer repo
3. Auto-discovered "contract" files that define LEGACY_/FORBIDDEN_/EXPECTED_
   constants whose values are packet match/old_param strings
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from conduit.prune.grep_imports import SKIP_DIRS

_CONTRACT_NAME_RE = re.compile(
    r"\b(?:LEGACY_|FORBIDDEN_|EXPECTED_|ALLOWED_|MODERN_|BASELINE_)[A-Z0-9_]*\b"
)


@dataclass
class IgnoreList:
    """Paths (posix relative) and literal patterns repair must not rewrite."""

    paths: set[str] = field(default_factory=set)
    globs: list[str] = field(default_factory=list)
    patterns: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)

    def path_ignored(self, rel: str) -> bool:
        rel_posix = rel.replace("\\", "/")
        if rel_posix in self.paths:
            return True
        name = Path(rel_posix).name
        for pattern in self.globs:
            if fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(name, pattern):
                return True
            # ``**/oracle.py`` should also match top-level ``oracle.py``
            if pattern.startswith("**/") and fnmatch.fnmatch(name, pattern[3:]):
                return True
            if pattern.startswith("**/") and fnmatch.fnmatch(rel_posix, pattern[3:]):
                return True
        return False

    def filter_paths(self, rels: Iterable[str]) -> list[str]:
        return [r for r in rels if not self.path_ignored(r)]

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "paths": sorted(self.paths),
            "globs": list(self.globs),
            "patterns": sorted(self.patterns),
            "reasons": dict(self.reasons),
        }


def _packet_match_strings(packet: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for rule in packet.get("rules") or []:
        rtype = str(rule.get("type") or "")
        if rtype == "EXACT_STRING_REPLACE":
            m = rule.get("match")
            if m:
                out.add(str(m))
        elif rtype == "AST_PARAM_RENAME":
            old = rule.get("old_param")
            if old:
                out.add(str(old))
        elif rtype == "AST_CALL_REWRITE":
            old = rule.get("old_callee")
            if old:
                out.add(str(old))
        elif rtype == "AST_ATTR_RENAME":
            old = rule.get("old_attr")
            if old:
                out.add(str(old))
        elif rtype == "KEY_RENAME":
            old = rule.get("old_key")
            if old:
                out.add(str(old))
    return out


def _merge_ignore_dict(target: IgnoreList, data: dict[str, Any], *, source: str) -> None:
    for g in data.get("globs") or []:
        g = str(g).strip()
        if g and g not in target.globs:
            target.globs.append(g)
            target.reasons[f"glob:{g}"] = source
    for p in data.get("paths") or []:
        rel = str(p).replace("\\", "/").lstrip("./")
        if rel:
            target.paths.add(rel)
            target.reasons[rel] = source
    for pat in data.get("patterns") or []:
        s = str(pat)
        if s:
            target.patterns.add(s)
            target.reasons[f"pattern:{s}"] = source


def _load_conduit_ignore_file(root: Path) -> dict[str, Any]:
    path = root / ".conduit" / "ignore.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".py", ".ts", ".js", ".tsx", ".jsx"}:
            yield path


def _is_contract_candidate(rel: str) -> bool:
    """Auto-ignore only test/oracle/policy contract files, never impl helpers."""
    posix = rel.replace("\\", "/")
    name = Path(posix).name.lower()
    if posix.startswith("tests/") or "/tests/" in posix:
        return True
    stem_bits = ("oracle", "contract", "policy", "forbidden", "expected", "baseline")
    return any(bit in name for bit in stem_bits)


def _literal_contract_assignment(text: str, match_strings: set[str]) -> bool:
    """True if a LEGACY_/FORBIDDEN_ name is bound to a quoted packet match string."""
    for line in text.splitlines():
        if not _CONTRACT_NAME_RE.search(line):
            continue
        for match in match_strings:
            if not match:
                continue
            if re.search(
                rf"""=\s*(['"]){re.escape(match)}\1""",
                line,
            ):
                return True
    return False


def discover_contract_files(root: Path, match_strings: set[str]) -> dict[str, str]:
    """
    Find files that look like migration-contract fixtures.

    A file is ignored when it lives under tests/ (or is named like an oracle/
    policy contract) AND literally assigns LEGACY_/FORBIDDEN_/… to a packet
    match string. Join-obfuscated impl helpers are never ignored.
    """
    if not match_strings:
        return {}
    found: dict[str, str] = {}
    root = root.resolve()
    for path in _iter_source_files(root):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if not _is_contract_candidate(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _CONTRACT_NAME_RE.search(text):
            continue
        if not _literal_contract_assignment(text, match_strings):
            continue
        found[rel] = "auto: contract constants (LEGACY_/FORBIDDEN_/EXPECTED_/…)"
    return found


def build_ignore_list(root: Path, packet: dict[str, Any]) -> IgnoreList:
    """Merge packet ignore, .conduit/ignore.json, and auto-discovered contract files."""
    ignore = IgnoreList()
    packet_ignore = packet.get("ignore")
    if isinstance(packet_ignore, dict):
        _merge_ignore_dict(ignore, packet_ignore, source="packet.ignore")
    file_ignore = _load_conduit_ignore_file(root)
    if file_ignore:
        _merge_ignore_dict(ignore, file_ignore, source=".conduit/ignore.json")

    matches = _packet_match_strings(packet) | set(ignore.patterns)
    for rel, reason in discover_contract_files(root, matches).items():
        ignore.paths.add(rel)
        ignore.reasons.setdefault(rel, reason)
        # Also treat those match strings as sensitive when they appear as contract values
        for m in matches:
            if m:
                ignore.patterns.add(m)

    return ignore


def line_is_contract_assignment(line: str, pattern: str) -> bool:
    """True if this line looks like a LEGACY_*/FORBIDDEN_* binding of ``pattern``."""
    if pattern not in line:
        return False
    if not _CONTRACT_NAME_RE.search(line):
        return False
    return True


def exact_replace_respecting_ignore(
    content: str, match: str, replace: str, *, ignored_patterns: set[str]
) -> tuple[str, int]:
    """
    Token-safe replace, but skip lines that bind ``match`` as a contract constant
    when ``match`` is in ignored_patterns.
    """
    from conduit.patcher.string_replace import exact_replace

    if match not in ignored_patterns or match not in content:
        return exact_replace(content, match, replace)

    lines = content.splitlines(keepends=True)
    out: list[str] = []
    count = 0
    for line in lines:
        if line_is_contract_assignment(line, match):
            out.append(line)
            continue
        updated, n = exact_replace(line, match, replace)
        count += n
        out.append(updated)
    return "".join(out), count
