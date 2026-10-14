"""Changelog lines as low-confidence hints.

A release note can name a rename or a deprecation. It does not prove the
signature. Higher-confidence wheel and schema diffs win when they already
cover the same old name.
"""

from __future__ import annotations

import re

from conduit.feeds.names import renames_from_text
from conduit.impact.model import ApiChange

_DEPRECATED = re.compile(
    r"(?<![\w.])(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+is deprecated\b",
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
    return changes
