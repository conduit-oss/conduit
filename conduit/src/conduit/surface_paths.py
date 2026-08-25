"""Prose/ops surfaces: leftover-oracle exclude, repair deny, post-green sync."""

from __future__ import annotations

import fnmatch
from pathlib import Path

# Surfaces synced after verify is green — not repaired mid self-correct.
PROSE_OPS_GLOBS: tuple[str, ...] = (
    "docs/**",
    "**/README*",
    "**/CHANGELOG*",
    "**/SCENARIOS*",
    "**/runbooks/**",
    "scripts/ops/**",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
)


def _posix(rel: str) -> str:
    return rel.replace("\\", "/")


def is_prose_ops_rel(rel: str) -> bool:
    """True for docs/README/scripts/ops/Dockerfile surfaces."""
    posix = _posix(rel)
    name = Path(posix).name
    for pat in PROSE_OPS_GLOBS:
        if fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(name, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(posix, pat[3:]):
            return True
        if "/" not in pat and name == pat:
            return True
    # docs/ prefix without glob match edge cases
    if posix.startswith("docs/") or "/docs/" in posix:
        return True
    if posix.startswith("scripts/ops/") or "/scripts/ops/" in posix:
        return True
    return False


def prose_ops_reject_reason(rel: str) -> str | None:
    if is_prose_ops_rel(rel):
        return (
            f"prose/ops path {_posix(rel)} is synced after green "
            "(not during self-correct)"
        )
    return None
