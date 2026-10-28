"""Shared types for ``conduit apply``."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SiteStatus(str, Enum):
    FIXED_VERIFIED = "fixed (verified)"
    FIXED_UNVERIFIED = "fixed (unverified: no tests)"
    #: The tests pass, but none of them runs the changed lines: the fix is unproven.
    FIXED_NOT_COVERED = "fixed, not covered by tests"
    NEEDS_HUMAN = "needs-human"
    DRY_RUN = "dry-run"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ImpactSite:
    """One call site from impact JSON, with the parent change as evidence."""

    path: str
    line: int
    expr: str
    dependency: str
    from_version: str
    to_version: str
    change: str
    confidence: str
    source: str
    kind: str = ""
    #: Optional release-diff fields when present on the impact record.
    symbol: str = ""
    old_signature: str = ""
    new_signature: str = ""
    new_docstring: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    candidates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    via_wrapper: bool = False

    @property
    def key(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class ContextPack:
    site: ImpactSite
    file_path: str
    imports: str
    function_name: str
    function_source: str
    function_start_line: int
    function_end_line: int
    full_source: str
    release_diff: dict[str, Any]
    new_signature: str
    new_docstring: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    #: Batch only: values other rewritten sites now pass into this function.
    connected: list[dict[str, Any]] = field(default_factory=list)
    #: The def's own decorators. They stay in the file as they are; the model
    #: sees the def without them, and copies it returns are dropped.
    decorators: list[str] = field(default_factory=list)
    #: Other uses in this file of a name this site's change removes (see
    #: ``context.name_uses``), with the type that now reaches them when known.
    removed_name_uses: list[dict[str, Any]] = field(default_factory=list)
    #: Batch only: finished new code of the sites this one depends on.
    producer_rewrites: list[dict[str, Any]] = field(default_factory=list)
    #: Keys a repo function in the rewrite reads from a dict, when any call there
    #: does. Empty for every other site, and omitted from the prompt then.
    dict_key_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": {
                "path": self.site.path,
                "line": self.site.line,
                "expr": self.site.expr,
                "dependency": self.site.dependency,
                "from_version": self.site.from_version,
                "to_version": self.site.to_version,
                "change": self.site.change,
                "confidence": self.site.confidence,
                "source": self.site.source,
                "kind": self.site.kind,
                "symbol": self.site.symbol,
                "via_wrapper": self.site.via_wrapper,
            },
            "file_path": self.file_path,
            "imports": self.imports,
            "function_name": self.function_name,
            "function_source": self.function_source,
            "function_start_line": self.function_start_line,
            "function_end_line": self.function_end_line,
            "release_diff": self.release_diff,
            "new_signature": self.new_signature,
            "new_docstring": self.new_docstring,
            "candidates": list(self.candidates),
        }


@dataclass
class SiteResult:
    site: ImpactSite
    status: SiteStatus
    why: str = ""
    diff: str = ""
    attempts: int = 0
    error: str = ""
    context: ContextPack | None = None
    #: Arguments the original call passed that the rewrite no longer does, and
    #: the one-sentence note about them (also appended to ``why``).
    dropped: list[str] = field(default_factory=list)
    dropped_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.site.path,
            "line": self.site.line,
            "expr": self.site.expr,
            "change": self.site.change,
            "status": self.status.value,
            "why": self.why,
            "diff": self.diff,
            "attempts": self.attempts,
            "error": self.error,
            "via_wrapper": self.site.via_wrapper,
            "dropped": list(self.dropped),
            "dropped_note": self.dropped_note,
        }
