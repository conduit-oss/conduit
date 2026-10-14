"""Impact records: one real change and the call sites it hits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiChange:
    """A difference between two released surfaces."""

    kind: str
    symbol: str
    old: str
    new: str
    detail: str
    confidence: str
    source: str
    service: str = ""
    #: Public names from the old wheel, such as ``openai.ChatCompletion.create``
    #: when ``symbol`` is the defining ``openai.api_resources...`` path.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteHit:
    path: str
    line: int
    expr: str
    confidence: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "expr": self.expr,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Impact:
    dependency: str
    from_version: str
    to_version: str
    change: str
    confidence: str
    source: str
    sites: tuple[SiteHit, ...]

    def to_dict(self) -> dict:
        return {
            "dependency": self.dependency,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "change": self.change,
            "confidence": self.confidence,
            "source": self.source,
            "sites": [site.to_dict() for site in self.sites],
        }
