"""Keep Conduit cache out of the consumer git status."""

from __future__ import annotations

from pathlib import Path

_ENTRY = ".conduit/"
_ALREADY = frozenset(
    {
        ".conduit/",
        ".conduit",
        "**/.conduit/",
        "**/.conduit",
        "/.conduit/",
        "/.conduit",
        ".conduit/**",
    }
)
_BLOCK = (
    "\n# Conduit cache (exports, verify-venv, packets)\n"
    f"{_ENTRY}\n"
)


def ensure_conduit_gitignore(root: Path, *, log=None) -> str | None:
    """Add ``.conduit/`` to the consumer ``.gitignore`` if missing.

    Returns the repo-relative path when a write happened, else ``None``.
    """
    root = root.resolve()
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return None
    if _already_ignored(existing):
        return None
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    try:
        path.write_text(existing + sep + _BLOCK.lstrip("\n"), encoding="utf-8")
    except OSError:
        return None
    if log:
        log("Added .conduit/ to .gitignore (exports, verify-venv, packets)")
    return ".gitignore"


def _already_ignored(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in _ALREADY:
            return True
    return False
