"""Public feeds for an outside service.

PyPI is ``https://pypi.org/pypi/{package}/json``. GitHub, npm, changelog,
and OpenAPI URLs are taken from a recorded fixture index or, when that
index is absent, left for a live fetch of the PyPI URL only. Nothing here
is a table of product names.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


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
) -> ServiceFeeds | None:
    """Feeds whose URLs are known.

    ``urls`` is a fixture index (URL → file). Only URLs in that index are
    attached, so an offline run does not request anything it cannot read.
    Without an index, the PyPI JSON URL is the one feed that needs no
    per-product configuration.
    """
    token = (service_id or "").strip()
    dist = (package or token).strip()
    if not token or not dist:
        return None
    if urls is None:
        return ServiceFeeds(service_id=token, package=dist, pypi=dist)
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
    return ServiceFeeds(
        service_id=token,
        package=dist,
        github_repo=github,
        pypi=pypi,
        npm=npm,
        changelog_url=changelog,
        openapi_previous_url=previous,
        openapi_latest_url=latest,
    )


def _mentions(url: str, name: str) -> bool:
    needle = (name or "").strip().lower()
    if len(needle) < 2:
        return False
    return needle in url.lower()
