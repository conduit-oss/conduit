"""Whole-repo AST index for apply rechecks.

Built once before the first site, then updated only for files a rewrite
changes. Declared/dict-key/typecheck lookups resolve through this index so
they still *see* every module, but they do not re-parse the tree per site.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

_SKIP_DIRS = {"tests", "test", ".venv", "venv", "node_modules"}

REPO_INDEX: ContextVar["RepoIndex | None"] = ContextVar("conduit_repo_index", default=None)


def module_names(rel: str) -> list[str]:
    """``pkg/sub/mod.py`` -> ``pkg.sub.mod``; a package's ``__init__.py`` -> the package."""
    dotted = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")
    if dotted.endswith(".__init__"):
        dotted = dotted[: -len(".__init__")]
    names = [dotted]
    if dotted.startswith("src."):
        names.append(dotted[len("src.") :])
    return names


@dataclass
class RepoIndex:
    """Parsed modules of one repo root, keyed by relative path and dotted name."""

    root: Path
    by_rel: dict[str, ast.Module] = field(default_factory=dict)
    by_module: dict[str, ast.Module] = field(default_factory=dict)
    #: Times ``ast.parse`` ran for this index (build + updates). Tests assert on it.
    parse_count: int = 0

    @classmethod
    def build(cls, root: Path) -> "RepoIndex":
        index = cls(root=root.resolve())
        for file in sorted(index.root.rglob("*.py")):
            if not file.is_file():
                continue
            try:
                rel = file.relative_to(index.root).as_posix()
            except ValueError:
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.split("/")[:-1]):
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            index._store(rel, text)
        return index

    def update(self, rel: str, source: str) -> None:
        """Re-parse one relative path from ``source`` (empty removes it)."""
        rel = rel.replace("\\", "/")
        self._drop(rel)
        if not source:
            return
        self._store(rel, source)

    def refresh_from_disk(self, rel: str) -> None:
        """Re-read and re-parse ``rel`` from the tree."""
        rel = rel.replace("\\", "/")
        path = self.root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            self._drop(rel)
            return
        self.update(rel, text)

    def modules_for(self, current: str, tree: ast.Module) -> dict[str, ast.Module]:
        """Namespace map: every indexed module, with ``current`` taken from ``tree``."""
        current = current.replace("\\", "/")
        out = dict(self.by_module)
        for name in module_names(current):
            out[name] = tree
        return out

    def iter_rels(self) -> list[str]:
        return sorted(self.by_rel)

    def _store(self, rel: str, source: str) -> None:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return
        self.parse_count += 1
        self.by_rel[rel] = tree
        for name in module_names(rel):
            self.by_module[name] = tree

    def _drop(self, rel: str) -> None:
        old = self.by_rel.pop(rel, None)
        if old is None:
            return
        for name in module_names(rel):
            if self.by_module.get(name) is old:
                del self.by_module[name]


@contextmanager
def using_index(index: RepoIndex | None) -> Iterator[RepoIndex | None]:
    token = REPO_INDEX.set(index)
    try:
        yield index
    finally:
        REPO_INDEX.reset(token)


def current_index() -> RepoIndex | None:
    return REPO_INDEX.get()
