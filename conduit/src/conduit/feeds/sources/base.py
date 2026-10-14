"""Plug-in surface for a feed source."""

from __future__ import annotations

from typing import Protocol

from conduit.feeds.model import FeedHit
from conduit.feeds.targets import WatchTarget
from conduit.feeds.client import FeedClient, FeedOfflineError


class FeedSource(Protocol):
    name: str
    confidence: str

    def collect(self, target: WatchTarget, client: FeedClient) -> list[FeedHit]:
        """Return hits for one outside service. Missing feeds yield no hits."""


def read_or_empty(client: FeedClient, url: str) -> str:
    if not url:
        return ""
    try:
        return client.get_text(url)
    except FeedOfflineError:
        return ""
