"""GitHub releases Atom feed for a service SDK repo."""

from __future__ import annotations

import feedparser

from conduit.feeds.client import FeedClient
from conduit.feeds.model import FeedHit
from conduit.feeds.names import plain_text, renames_from_text
from conduit.feeds.sources.base import read_or_empty
from conduit.feeds.targets import WatchTarget
from conduit.feeds.versions import is_newer, latest_stable, parse_version, version_text


class GitHubReleasesSource:
    name = "github_releases"
    confidence = "high"

    def collect(self, target: WatchTarget, client: FeedClient) -> list[FeedHit]:
        url = target.feeds.github_atom_url()
        text = read_or_empty(client, url)
        if not text:
            return []
        return parse_github_atom(
            text,
            url=url,
            package=target.feeds.pypi or target.package,
            installed=target.pypi_version,
        )


def parse_github_atom(
    text: str,
    *,
    url: str,
    package: str,
    installed: str | None,
) -> list[FeedHit]:
    parsed = feedparser.parse(text)
    tagged: list[tuple[object, str]] = []
    for entry in parsed.entries or []:
        title = str(entry.get("title") or "")
        version = parse_version(title)
        if version is None:
            continue
        tagged.append((entry, version))
    if not tagged:
        return []
    latest = latest_stable([version for _entry, version in tagged])
    if latest is None:
        return []
    newest = next(entry for entry, version in tagged if version == latest)
    from_version = _from_version(installed, tagged, latest)
    if not from_version:
        return []
    if installed and not is_newer(latest, installed):
        return []
    if not installed and parse_version(from_version) is not None:
        if not is_newer(latest, from_version):
            return []
    to_version = version_text(latest)
    hits = [
        FeedHit(
            source="github_releases",
            confidence="high",
            package=package,
            ecosystem="pypi",
            from_version=from_version,
            to_version=to_version,
            change="version",
            old=from_version,
            new=to_version,
            detail=f"GitHub release {to_version} on {url}",
            url=str(newest.get("link") or url),
        )
    ]
    body = _entry_body(newest)
    hits.extend(
        _rename_hits(
            body,
            package=package,
            from_version=from_version,
            to_version=to_version,
            url=str(newest.get("link") or url),
        )
    )
    return hits


def _from_version(installed: str | None, tagged: list[tuple[object, object]], latest: object) -> str:
    if installed and parse_version(installed) is not None:
        return str(parse_version(installed))
    older = [version for _entry, version in tagged if version < latest and not version.is_prerelease]
    if not older:
        older = [version for _entry, version in tagged if version < latest]
    if not older:
        return ""
    return version_text(max(older))


def _entry_body(entry: object) -> str:
    parts = [str(entry.get("title") or "")]
    content = entry.get("content") or []
    if content and isinstance(content, list):
        parts.append(str(content[0].get("value") or ""))
    parts.append(str(entry.get("summary") or ""))
    return plain_text("\n".join(parts))


def _rename_hits(
    body: str,
    *,
    package: str,
    from_version: str,
    to_version: str,
    url: str,
) -> list[FeedHit]:
    calls, params = renames_from_text(body)
    new_callee = calls[0][1] if calls else ""
    hits: list[FeedHit] = []
    for old, new in calls:
        hits.append(
            FeedHit(
                source="github_releases",
                confidence="medium",
                package=package,
                ecosystem="pypi",
                from_version=from_version,
                to_version=to_version,
                change="call_rename",
                old=old,
                new=new,
                detail=f"{old} renamed to {new}",
                url=url,
            )
        )
    for old, new in params:
        hits.append(
            FeedHit(
                source="github_releases",
                confidence="medium",
                package=package,
                ecosystem="pypi",
                from_version=from_version,
                to_version=to_version,
                change="param_rename",
                old=old,
                new=new,
                detail=f"parameter {old} renamed to {new}",
                url=url,
                target=new_callee,
            )
        )
    return hits
