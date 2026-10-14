"""PyPI JSON version check."""

from __future__ import annotations

import json

from conduit.feeds.client import FeedClient
from conduit.feeds.model import FeedHit
from conduit.feeds.sources.base import read_or_empty
from conduit.feeds.targets import WatchTarget
from conduit.feeds.versions import is_newer, latest_stable, parse_version, version_text


class PyPISource:
    name = "pypi"
    confidence = "high"

    def collect(self, target: WatchTarget, client: FeedClient) -> list[FeedHit]:
        url = target.feeds.pypi_url()
        text = read_or_empty(client, url)
        if not text:
            return []
        return parse_pypi_json(
            text,
            url=url,
            package=target.feeds.pypi or target.package,
            installed=target.pypi_version,
        )


def parse_pypi_json(
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
    releases = payload.get("releases")
    if isinstance(releases, dict):
        for key in releases:
            parsed = parse_version(str(key))
            if parsed is not None:
                versions.append(parsed)
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    info_version = parse_version(str(info.get("version") or ""))
    if info_version is not None:
        versions.append(info_version)
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
            source="pypi",
            confidence="high",
            package=package,
            ecosystem="pypi",
            from_version=from_version,
            to_version=to_version,
            change="version",
            old=from_version,
            new=to_version,
            detail=f"PyPI {package} {from_version} -> {to_version}",
            url=url,
        )
    ]
