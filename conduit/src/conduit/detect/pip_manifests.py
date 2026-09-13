"""Discover pip requirements/constraints files under a repo root."""

from __future__ import annotations

from pathlib import Path

from conduit.prune.grep_imports import SKIP_DIRS


def _path_skipped(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in SKIP_DIRS for part in rel_parts)


def _depth_key(path: Path, root: Path) -> tuple[int, str]:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return (10_000, path.as_posix().lower())
    return (len(rel.parts), rel.as_posix().lower())


def iter_pip_manifests(root: Path, *, scope: str = "main") -> list[Path]:
    """Root plus nested requirements/constraints files (venv/vendor skipped).

    Results are ordered shallowest-first so callers can prefer root pins.
    """
    root = root.resolve()
    names = (
        {"requirements-dev.txt"}
        if scope == "dev"
        else {"requirements.txt", "constraints.txt"}
    )
    name_set = {n.lower() for n in names}
    found: list[Path] = []
    seen: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file() or _path_skipped(path, root):
            continue
        name = path.name.lower()
        if name in name_set or (
            scope != "dev"
            and name.endswith(".txt")
            and "requirements" in name
            and name != "requirements-dev.txt"
        ):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(path)
    found.sort(key=lambda p: _depth_key(p, root))
    return found
