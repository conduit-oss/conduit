"""Public feeds for an outside service.

PyPI is ``https://pypi.org/pypi/{package}/json``. A recorded fixture index
attaches only the URLs it already has. Without that index, live discovery
reads the PyPI project page, follows its source repository, and derives the
release feed, a changelog file at the release tags, and versioned API specs
at both tags. npm is attached only when the project metadata links to it.
Nothing here is a table of product names.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from conduit.feeds.versions import latest_stable, parse_version, version_text

Fetch = Callable[[str], tuple[str | None, str]]
SpecAtRef = Callable[[str, str], tuple[str | None, str]]

_CHANGELOG_NAMES = (
    "changelog.md",
    "changelog.rst",
    "changelog.txt",
    "changelog",
    "changes.md",
    "changes.rst",
    "changes.txt",
    "changes",
    "history.md",
    "history.rst",
    "history.txt",
    "history",
    "news.md",
    "news.rst",
    "news.txt",
    "news",
)
_CHANGELOG_LABELS = ("changelog", "changes", "history", "news", "release")


@dataclass(frozen=True)
class ServiceFeeds:
    service_id: str
    package: str
    github_repo: str = ""
    pypi: str = ""
    npm: str = ""
    changelog_url: str = ""
    openapi_previous_url: str = ""
    openapi_latest_url: str = ""
    releases_api: str = ""
    notes: tuple[str, ...] = ()

    def github_atom_url(self) -> str:
        if not self.github_repo:
            return ""
        return f"https://github.com/{self.github_repo}/releases.atom"

    def pypi_url(self) -> str:
        if not self.pypi:
            return ""
        return f"https://pypi.org/pypi/{self.pypi}/json"

    def npm_url(self) -> str:
        if not self.npm:
            return ""
        return f"https://registry.npmjs.org/{self.npm}"


def feeds_for(
    service_id: str,
    package: str = "",
    *,
    urls: dict[str, str] | None = None,
    from_version: str = "",
    fetch: Fetch | None = None,
    spec_at_ref: SpecAtRef | None = None,
) -> ServiceFeeds | None:
    """Feeds whose URLs are known.

    ``urls`` is a fixture index (URL → file). Only URLs in that index are
    attached, so an offline run does not request anything it cannot read.
    Without an index, feeds are discovered from the package's project page.
    """
    token = (service_id or "").strip()
    dist = (package or token).strip()
    if not token or not dist:
        return None
    if urls is None:
        return discover_live_feeds(
            dist,
            token,
            from_version=from_version,
            fetch=fetch,
            spec_at_ref=spec_at_ref,
        )
    matched = [url for url in urls if _mentions(url, dist) or _mentions(url, token)]
    github = ""
    pypi = ""
    npm = ""
    previous = ""
    latest = ""
    changelog = ""
    pypi_url = f"https://pypi.org/pypi/{dist}/json"
    if pypi_url in urls:
        pypi = dist
    for url in matched:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        lowered = url.lower()
        if host == "github.com" and path.endswith("/releases.atom"):
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 3:
                github = f"{parts[0]}/{parts[1]}"
            continue
        if "registry.npmjs.org" in host:
            name = path.strip("/")
            if name:
                npm = name
            continue
        if path.endswith("openapi.yaml") or path.endswith("openapi.yml") or "openapi" in lowered:
            if "previous" in lowered and not previous:
                previous = url
            elif "latest" in lowered and not latest:
                latest = url
            continue
        if url == pypi_url:
            continue
        if not changelog:
            changelog = url
    if not any((github, pypi, npm, previous, latest, changelog)):
        return None
    releases_api = f"https://api.github.com/repos/{github}/releases" if github else ""
    return ServiceFeeds(
        service_id=token,
        package=dist,
        github_repo=github,
        pypi=pypi,
        npm=npm,
        changelog_url=changelog,
        openapi_previous_url=previous,
        openapi_latest_url=latest,
        releases_api=releases_api,
    )


def discover_live_feeds(
    package: str,
    service_id: str,
    *,
    from_version: str = "",
    fetch: Fetch | None = None,
    spec_at_ref: SpecAtRef | None = None,
) -> ServiceFeeds:
    """Discover feeds from PyPI project metadata. ``fetch`` is injected in tests."""
    getter = fetch or _default_fetch
    spec_lookup = spec_at_ref or _default_spec_at_ref
    notes: list[str] = []
    pinned = (from_version or "").strip()
    meta_url = f"https://pypi.org/pypi/{package}/json"
    body, reason = getter(meta_url)
    if not body:
        return ServiceFeeds(
            service_id=service_id,
            package=package,
            pypi=package,
            notes=(
                f"not compared: {package} project metadata could not be fetched ({reason or 'unknown'})",
            ),
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ServiceFeeds(
            service_id=service_id,
            package=package,
            pypi=package,
            notes=(f"not compared: {package} project metadata could not be read",),
        )
    if not isinstance(payload, dict):
        return ServiceFeeds(
            service_id=service_id,
            package=package,
            pypi=package,
            notes=(f"not compared: {package} project metadata could not be read",),
        )
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    from conduit.impact.openapi_sources import github_repo_from_project_info, project_links

    links = project_links(info)
    repo = github_repo_from_project_info(info) or ""
    npm = _npm_name(links)
    latest = _latest_release(payload)
    changelog = ""
    if repo:
        changelog, changelog_note = _changelog_at_tags(
            repo,
            package,
            _version_order(latest, pinned),
            getter,
        )
        if changelog_note:
            notes.append(changelog_note)
    if not changelog:
        linked = _changelog_link(info)
        if linked:
            changelog = linked
        elif repo and not any(note.startswith("not compared: changelog") for note in notes):
            notes.append(f"not compared: no changelog file at release tags for {package}")
    previous = ""
    newest = ""
    spec_to = latest if latest and latest != pinned else ""
    if repo and pinned and spec_to:
        previous, newest, spec_note = _spec_urls(package, repo, pinned, spec_to, spec_lookup)
        if spec_note:
            notes.append(spec_note)
    releases_api = f"https://api.github.com/repos/{repo}/releases" if repo else ""
    return ServiceFeeds(
        service_id=service_id,
        package=package,
        github_repo=repo,
        pypi=package,
        npm=npm,
        changelog_url=changelog,
        openapi_previous_url=previous,
        openapi_latest_url=newest,
        releases_api=releases_api,
        notes=tuple(notes),
    )


def _default_fetch(url: str) -> tuple[str | None, str]:
    from conduit.feeds.http import cached_get

    return cached_get(url)


def _default_spec_at_ref(repo: str, ref: str) -> tuple[str | None, str]:
    from conduit.impact.openapi_sources import discover_spec_at_ref

    return discover_spec_at_ref(repo, ref)


def _latest_release(payload: dict) -> str:
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
    return version_text(latest) if latest is not None else ""


def _version_order(latest: str, pinned: str) -> list[str]:
    ordered: list[str] = []
    for version in (latest, pinned):
        if version and version not in ordered:
            ordered.append(version)
    return ordered


def _spec_urls(
    package: str,
    repo: str,
    from_version: str,
    to_version: str,
    spec_lookup: SpecAtRef,
) -> tuple[str, str, str]:
    from conduit.impact.openapi_sources import spec_location_to_url

    old_loc, old_why = spec_lookup(repo, from_version)
    new_loc, new_why = spec_lookup(repo, to_version)
    previous = spec_location_to_url(old_loc or "")
    latest = spec_location_to_url(new_loc or "")
    if previous and latest:
        return previous, latest, ""
    why = "; ".join(part for part in (old_why, new_why) if part) or "unknown"
    return (
        "",
        "",
        (
            f"not compared: no OpenAPI/Swagger/discovery spec at tags for "
            f"{package} ({repo}) {from_version!r} → {to_version!r} ({why})"
        ),
    )


def _changelog_at_tags(
    repo: str,
    package: str,
    versions: list[str],
    fetch: Fetch,
) -> tuple[str, str]:
    from conduit.impact.openapi_sources import _tag_candidates

    for version in versions:
        for tag in _tag_candidates(version):
            body, reason = fetch(_contents_url(repo, "", tag))
            if body is None:
                if _missing(reason):
                    continue
                return "", _changelog_fetch_note(package, reason)
            found = _pick_changelog(body, repo, tag)
            if found:
                return found, ""
            docs_body, docs_reason = fetch(_contents_url(repo, "docs", tag))
            if docs_body is None:
                if _missing(docs_reason):
                    continue
                return "", _changelog_fetch_note(package, docs_reason)
            found = _pick_changelog(docs_body, repo, tag)
            if found:
                return found, ""
    return "", ""


def _contents_url(repo: str, path: str, ref: str) -> str:
    base = f"https://api.github.com/repos/{repo}/contents"
    if path:
        base += "/" + quote(path, safe="/")
    return f"{base}?ref={quote(ref, safe='')}"


def _pick_changelog(body: str, repo: str, tag: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return ""
    by_name: dict[str, dict] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {None, "file"}:
            continue
        name = str(item.get("name") or "").lower()
        if name:
            by_name[name] = item
    for name in _CHANGELOG_NAMES:
        item = by_name.get(name)
        if not item:
            continue
        download = str(item.get("download_url") or "").strip()
        if download:
            return download
        path = str(item.get("path") or name).lstrip("/")
        return f"https://raw.githubusercontent.com/{repo}/{quote(tag, safe='')}/{path}"
    return ""


def _changelog_link(info: dict) -> str:
    project_urls = info.get("project_urls") if isinstance(info, dict) else None
    if not isinstance(project_urls, dict):
        return ""
    for key, value in project_urls.items():
        if not isinstance(value, str) or not value.strip():
            continue
        label = str(key).lower()
        if any(word in label for word in _CHANGELOG_LABELS):
            return value.strip()
    return ""


def _npm_name(urls: list[str]) -> str:
    for url in urls:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        parts = [part for part in (parsed.path or "").split("/") if part]
        if host == "registry.npmjs.org":
            if parts[:1] == ["package"]:
                parts = parts[1:]
            return _scoped_name(parts)
        if host in {"www.npmjs.com", "npmjs.com", "www.npmjs.org", "npmjs.org"}:
            if "package" not in parts:
                continue
            return _scoped_name(parts[parts.index("package") + 1 :])
    return ""


def _scoped_name(parts: list[str]) -> str:
    if not parts:
        return ""
    if parts[0].startswith("@") and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _changelog_fetch_note(package: str, reason: str) -> str:
    return f"not compared: changelog for {package} could not be fetched ({reason or 'unknown'})"


def _missing(reason: str) -> bool:
    text = (reason or "").lower()
    return "404" in text or "400" in text


def _mentions(url: str, name: str) -> bool:
    needle = (name or "").strip().lower()
    if len(needle) < 2:
        return False
    return needle in url.lower()
