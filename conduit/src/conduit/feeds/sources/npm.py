"""npm registry version check. Skipped when the repo has no npm pin."""

from __future__ import annotations

import json

from conduit.feeds.client import FeedClient
from conduit.feeds.model import FeedHit
from conduit.feeds.sources.base import read_or_empty
from conduit.feeds.targets import WatchTarget
from conduit.feeds.versions import is_newer, latest_stable, parse_version, version_text


class NpmSource:
    name = "npm"
    confidence = "high"

    def collect(self, target: WatchTarget, client: FeedClient) -> list[FeedHit]:
        if not target.npm_version or not target.feeds.npm:
            return []
        url = target.feeds.npm_url()
        text = read_or_empty(client, url)
        if not text:
            return []
        return parse_npm_json(
            text,
            url=url,
            package=target.feeds.npm,
            installed=target.npm_version,
        )


def parse_npm_json(
    text: str,
    *,
    url: str,
    package: str,
    installed: str | None,
) -> list[FeedHit]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    versions = []
    raw_versions = payload.get("versions")
    if isinstance(raw_versions, dict):
        for key in raw_versions:
            parsed = parse_version(str(key))
            if parsed is not None:
                versions.append(parsed)
    dist = payload.get("dist-tags") if isinstance(payload.get("dist-tags"), dict) else {}
    tagged = parse_version(str(dist.get("latest") or ""))
    if tagged is not None:
        versions.append(tagged)
    latest = latest_stable(versions)
    if latest is None or not installed or not is_newer(latest, installed):
        return []
    current = parse_version(installed)
    if current is None:
        return []
    to_version = version_text(latest)
    from_version = version_text(current)
    return [
        FeedHit(
            source="npm",
            confidence="high",
            package=package,
            ecosystem="npm",
            from_version=from_version,
            to_version=to_version,
            change="version",
            old=from_version,
            new=to_version,
            detail=f"npm {package} {from_version} -> {to_version}",
            url=url,
        )
    ]
