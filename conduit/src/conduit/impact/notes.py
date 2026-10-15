"""Changelog lines as low-confidence hints.

A release note can name a rename or a deprecation. It does not prove the
signature. Higher-confidence wheel and schema diffs win when they already
cover the same old name.
"""

from __future__ import annotations

import re

from conduit.feeds.names import plain_text, renames_from_text
from conduit.impact.model import ApiChange

_DEPRECATED = re.compile(
    r"(?<![\w.])(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+is deprecated\b",
    re.IGNORECASE,
)
_ENDPOINT_GONE = re.compile(
    r"\b(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD)\s+`?(?P<path>/[A-Za-z0-9_{}.\-/]+)`?\s+"
    r"(?:has been\s+)?(?P<verb>removed|deprecated)\b",
    re.IGNORECASE,
)
_ENDPOINT_RENAME = re.compile(
    r"`?(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD)\s+(?P<old>/[A-Za-z0-9_{}.\-/]+)`?\s+"
    r"(?:has been\s+)?renamed to\s+`?(?:(?P<new_method>GET|POST|PUT|PATCH|DELETE|HEAD)\s+)?"
    r"(?P<new>/[A-Za-z0-9_{}.\-/]+)`?",
    re.IGNORECASE,
)
_PATH_GONE = re.compile(
    r"\b(?P<verb>removed|deprecated)\s+`?(?P<path>/v[0-9][A-Za-z0-9_{}.\-/]*)`?",
    re.IGNORECASE,
)


def changes_from_notes(text: str) -> list[ApiChange]:
    calls, params = renames_from_text(text or "")
    changes: list[ApiChange] = []
    for old, new in calls:
        changes.append(
            ApiChange(
                kind="renamed",
                symbol=old,
                old=old,
                new=new,
                detail=f"{old} renamed to {new}",
                confidence="low",
                source="changelog",
            )
        )
    for old, new in params:
        changes.append(
            ApiChange(
                kind="param_renamed",
                symbol=old,
                old=old,
                new=new,
                detail=f"parameter {old} renamed to {new}",
                confidence="low",
                source="changelog",
            )
        )
    for match in _DEPRECATED.finditer(text or ""):
        name = match.group("name")
        changes.append(
            ApiChange(
                kind="deprecated",
                symbol=name,
                old=name,
                new=name,
                detail=f"{name} is deprecated",
                confidence="low",
                source="changelog",
            )
        )
    changes.extend(endpoint_changes_from_notes(text or ""))
    return changes


def endpoint_changes_from_notes(text: str) -> list[ApiChange]:
    """Endpoint removals and renames stated in a changelog. Low confidence."""
    body = plain_text(text or "")
    changes: list[ApiChange] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _ENDPOINT_RENAME.finditer(body):
        method = match.group("method").upper()
        old = match.group("old")
        new = match.group("new")
        new_method = (match.group("new_method") or method).upper()
        key = ("endpoint_renamed", f"{method} {old}", new)
        if key in seen or old == new:
            continue
        seen.add(key)
        changes.append(
            ApiChange(
                kind="endpoint_renamed",
                symbol=f"{method} {old}",
                old=old,
                new=new,
                detail=f"{method} {old} renamed to {new_method} {new}",
                confidence="low",
                source="changelog",
                service="",
            )
        )
    for match in _ENDPOINT_GONE.finditer(body):
        method = match.group("method").upper()
        path = match.group("path")
        key = ("endpoint_removed", f"{method} {path}", "")
        if key in seen:
            continue
        seen.add(key)
        changes.append(
            ApiChange(
                kind="endpoint_removed",
                symbol=f"{method} {path}",
                old=path,
                new="",
                detail=f"{method} {path} removed",
                confidence="low",
                source="changelog",
                service="",
            )
        )
    for match in _PATH_GONE.finditer(body):
        path = match.group("path")
        key = ("endpoint_removed", path, "")
        if any(item[1].endswith(" " + path) or item[1].endswith(path) for item in seen):
            continue
        seen.add(key)
        changes.append(
            ApiChange(
                kind="endpoint_removed",
                symbol=f"* {path}",
                old=path,
                new="",
                detail=f"{path} removed",
                confidence="low",
                source="changelog",
                service="",
            )
        )
    return changes
