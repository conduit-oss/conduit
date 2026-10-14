"""Changelog or deprecation page. Lowest confidence: it is prose."""

from __future__ import annotations

from conduit.feeds.client import FeedClient
from conduit.feeds.model import FeedHit
from conduit.feeds.names import plain_text, renames_from_text
from conduit.feeds.sources.base import read_or_empty
from conduit.feeds.targets import WatchTarget


class ChangelogSource:
    name = "changelog"
    confidence = "low"

    def collect(self, target: WatchTarget, client: FeedClient) -> list[FeedHit]:
        url = target.feeds.changelog_url
        text = read_or_empty(client, url)
        if not text:
            return []
        ecosystem = "pypi" if target.feeds.pypi or target.pypi_version else "npm"
        package = target.feeds.pypi or target.feeds.npm or target.package
        return parse_changelog(text, url=url, package=package, ecosystem=ecosystem)


def parse_changelog(
    text: str,
    *,
    url: str,
    package: str,
    ecosystem: str,
) -> list[FeedHit]:
    calls, params = renames_from_text(plain_text(text))
    new_callee = calls[0][1] if calls else ""
    hits: list[FeedHit] = []
    for old, new in calls:
        hits.append(
            FeedHit(
                source="changelog",
                confidence="low",
                package=package,
                ecosystem=ecosystem,
                from_version="",
                to_version="",
                change="call_rename",
                old=old,
                new=new,
                detail=f"changelog says {old} renamed to {new}",
                url=url,
            )
        )
    for old, new in params:
        hits.append(
            FeedHit(
                source="changelog",
                confidence="low",
                package=package,
                ecosystem=ecosystem,
                from_version="",
                to_version="",
                change="param_rename",
                old=old,
                new=new,
                detail=f"changelog says parameter {old} renamed to {new}",
                url=url,
                target=new_callee,
            )
        )
    return hits
