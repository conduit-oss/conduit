"""One outside service on a customer's map, plus the versions they have pinned."""

from __future__ import annotations

from dataclasses import dataclass

from conduit.feeds.lookup import ServiceFeeds


@dataclass(frozen=True)
class WatchTarget:
    service_id: str
    package: str
    pypi_version: str | None
    npm_version: str | None
    feeds: ServiceFeeds
