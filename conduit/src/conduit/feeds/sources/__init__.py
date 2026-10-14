"""Built-in feed sources. Pass a different list to ``watch_outside`` to plug in another."""

from __future__ import annotations

from conduit.feeds.sources.base import FeedSource
from conduit.feeds.sources.changelog import ChangelogSource
from conduit.feeds.sources.github_releases import GitHubReleasesSource
from conduit.feeds.sources.npm import NpmSource
from conduit.feeds.sources.openapi import OpenAPISource
from conduit.feeds.sources.pypi import PyPISource

_EXTRA: list[FeedSource] = []


def register_source(source: FeedSource) -> None:
    """Add a source for later ``default_sources()`` calls in this process."""
    _EXTRA.append(source)


def default_sources() -> list[FeedSource]:
    return [
        GitHubReleasesSource(),
        PyPISource(),
        NpmSource(),
        OpenAPISource(),
        ChangelogSource(),
        *list(_EXTRA),
    ]


__all__ = [
    "ChangelogSource",
    "FeedSource",
    "GitHubReleasesSource",
    "NpmSource",
    "OpenAPISource",
    "PyPISource",
    "default_sources",
    "register_source",
]
