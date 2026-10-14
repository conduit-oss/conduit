"""Records produced by one feed read."""

from __future__ import annotations

from dataclasses import dataclass


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class FeedHit:
    """One change noticed on a public feed.

    ``confidence`` is high for a version a registry or release feed states,
    medium for an OpenAPI diff or a rename parsed out of release notes, and
    low for a changelog or deprecation page.
    """

    source: str
    confidence: str
    package: str
    ecosystem: str
    from_version: str
    to_version: str
    change: str
    old: str
    new: str
    detail: str
    url: str
    target: str = ""

    def rank(self) -> int:
        return CONFIDENCE_RANK.get(self.confidence, 0)
