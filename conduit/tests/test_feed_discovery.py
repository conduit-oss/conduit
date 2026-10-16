"""Live feed discovery with mocked HTTP. No sockets."""

from __future__ import annotations

import json

import httpx
import pytest

from conduit.feeds.client import FeedClient
from conduit.feeds.http import cached_get, clear_feed_cache
from conduit.feeds.lookup import ServiceFeeds, discover_live_feeds, feeds_for
from conduit.feeds.sources.github_releases import GitHubReleasesSource, parse_github_releases_api
from conduit.feeds.targets import WatchTarget
from conduit.impact.openapi_sources import spec_location_to_url

PACKAGE = "example-widget"
REPO = "example/example-widget"
PYPI = f"https://pypi.org/pypi/{PACKAGE}/json"


def _meta(*, version: str = "2.0.0", urls: dict | None = None, home: str = "") -> str:
    info = {
        "version": version,
        "project_urls": urls
        or {
            "Source": f"https://github.com/{REPO}",
        },
    }
    if home:
        info["home_page"] = home
    releases = {"1.0.0": [], version: [], "2.1.0a1": []}
    return json.dumps({"info": info, "releases": releases})


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_feed_cache()
    yield
    clear_feed_cache()


def test_live_discovery_attaches_repo_changelog_and_specs():
    def fetch(url: str):
        if url == PYPI:
            return _meta(), ""
        if url == f"https://api.github.com/repos/{REPO}/contents?ref=2.0.0":
            return (
                json.dumps(
                    [
                        {"name": "README.md", "type": "file"},
                        {
                            "name": "CHANGELOG.md",
                            "type": "file",
                            "download_url": f"https://raw.githubusercontent.com/{REPO}/2.0.0/CHANGELOG.md",
                        },
                    ]
                ),
                "",
            )
        return None, "HTTP 404"

    def spec(repo: str, ref: str):
        assert repo == REPO
        return f"raw:{repo}@{ref}/openapi.yaml", ""

    feeds = discover_live_feeds(
        PACKAGE,
        PACKAGE,
        from_version="1.0.0",
        fetch=fetch,
        spec_at_ref=spec,
    )
    assert feeds is not None
    assert feeds.github_repo == REPO
    assert feeds.github_atom_url() == f"https://github.com/{REPO}/releases.atom"
    assert feeds.releases_api == f"https://api.github.com/repos/{REPO}/releases"
    assert feeds.changelog_url == f"https://raw.githubusercontent.com/{REPO}/2.0.0/CHANGELOG.md"
    assert feeds.openapi_previous_url == f"https://raw.githubusercontent.com/{REPO}/1.0.0/openapi.yaml"
    assert feeds.openapi_latest_url == f"https://raw.githubusercontent.com/{REPO}/2.0.0/openapi.yaml"
    assert feeds.npm == ""
    assert feeds.pypi == PACKAGE
    assert feeds.notes == ()


def test_npm_only_when_metadata_links_to_it():
    def fetch(url: str):
        if url == PYPI:
            return (
                _meta(
                    version="1.2.0",
                    urls={
                        "Source": f"https://github.com/{REPO}",
                        "npm": "https://www.npmjs.com/package/@example/widget",
                    },
                ),
                "",
            )
        return None, "HTTP 404"

    feeds = discover_live_feeds(PACKAGE, PACKAGE, fetch=fetch, spec_at_ref=lambda *_: (None, "unused"))
    assert feeds.npm == "@example/widget"
    assert feeds.npm_url() == "https://registry.npmjs.org/@example/widget"


def test_npm_name_is_not_guessed_from_the_package_name():
    def fetch(url: str):
        return _meta(version="1.0.0", urls={"Homepage": "https://example.com/widget"}), ""

    feeds = discover_live_feeds(PACKAGE, PACKAGE, fetch=fetch)
    assert feeds.npm == ""
    assert feeds.github_repo == ""


def test_pypi_timeout_is_not_compared_and_skips_spec_lookup():
    def fetch(url: str):
        return None, "timed out"

    def spec(*_args):
        raise AssertionError("spec lookup")

    feeds = discover_live_feeds(PACKAGE, PACKAGE, from_version="1.0.0", fetch=fetch, spec_at_ref=spec)
    assert feeds.pypi == PACKAGE
    assert feeds.github_repo == ""
    assert feeds.notes == (
        f"not compared: {PACKAGE} project metadata could not be fetched (timed out)",
    )


def test_missing_spec_is_not_compared_and_changelog_link_is_kept():
    def fetch(url: str):
        if url == PYPI:
            return (
                _meta(
                    urls={
                        "Source": f"https://github.com/{REPO}",
                        "Changelog": "https://example.com/changes",
                    }
                ),
                "",
            )
        return None, "HTTP 404"

    def spec(repo: str, ref: str):
        return None, f"{ref}: no OpenAPI/Swagger/discovery file at common paths"

    feeds = discover_live_feeds(
        PACKAGE,
        PACKAGE,
        from_version="1.0.0",
        fetch=fetch,
        spec_at_ref=spec,
    )
    assert feeds.openapi_previous_url == ""
    assert feeds.openapi_latest_url == ""
    assert feeds.changelog_url == "https://example.com/changes"
    assert any(note.startswith("not compared: no OpenAPI/Swagger/discovery spec") for note in feeds.notes)
    assert any("1.0.0" in note and "2.0.0" in note for note in feeds.notes)
    assert not any("no changelog file" in note for note in feeds.notes)


def test_changelog_timeout_is_not_compared():
    def fetch(url: str):
        if url == PYPI:
            return _meta(), ""
        return None, "HTTP 429"

    feeds = discover_live_feeds(PACKAGE, PACKAGE, from_version="1.0.0", fetch=fetch, spec_at_ref=lambda *_: (None, ""))
    assert any("not compared: changelog" in note and "HTTP 429" in note for note in feeds.notes)
    assert not any("no changelog file" in note for note in feeds.notes)


def test_no_changelog_file_is_not_compared():
    def fetch(url: str):
        if url == PYPI:
            return _meta(), ""
        if "/contents" in url and "/docs" not in url:
            return json.dumps([{"name": "README.md", "type": "file"}]), ""
        return None, "HTTP 404"

    feeds = discover_live_feeds(PACKAGE, PACKAGE, from_version="1.0.0", fetch=fetch, spec_at_ref=lambda *_: (None, ""))
    assert feeds.changelog_url == ""
    assert f"not compared: no changelog file at release tags for {PACKAGE}" in feeds.notes


def test_fixture_index_does_not_fetch():
    def fetch(url: str):
        raise AssertionError(url)

    recorded = {PYPI: "pypi/example-widget.json"}
    feeds = feeds_for(PACKAGE, PACKAGE, urls=recorded, from_version="1.0.0", fetch=fetch)
    assert feeds is not None
    assert feeds.pypi == PACKAGE
    assert feeds.github_repo == ""
    assert feeds.notes == ()


def test_spec_location_becomes_an_https_url():
    assert (
        spec_location_to_url(f"raw:{REPO}@v1.2.0/openapi.yaml")
        == f"https://raw.githubusercontent.com/{REPO}/v1.2.0/openapi.yaml"
    )
    assert spec_location_to_url("url:https://example.com/openapi.yaml") == "https://example.com/openapi.yaml"


def test_releases_api_parses_renames_and_skips_prerelease():
    text = json.dumps(
        [
            {
                "tag_name": "v2.0.0rc1",
                "prerelease": True,
                "body": "limit renamed to page_size",
            },
            {
                "tag_name": "v1.2.0",
                "prerelease": False,
                "name": "one",
                "body": "limit renamed to page_size",
                "html_url": f"https://github.com/{REPO}/releases/tag/v1.2.0",
            },
            {"tag_name": "v1.0.0", "prerelease": False, "body": ""},
        ]
    )
    hits = parse_github_releases_api(
        text,
        url=f"https://api.github.com/repos/{REPO}/releases",
        package=PACKAGE,
        installed="1.0.0",
    )
    version = next(hit for hit in hits if hit.change == "version")
    assert version.to_version == "1.2.0"
    renamed = next(hit for hit in hits if hit.change == "param_rename")
    assert (renamed.old, renamed.new) == ("limit", "page_size")


def _target() -> WatchTarget:
    feeds = ServiceFeeds(
        service_id=PACKAGE,
        package=PACKAGE,
        pypi=PACKAGE,
        github_repo=REPO,
        releases_api=f"https://api.github.com/repos/{REPO}/releases",
    )
    return WatchTarget(
        service_id=PACKAGE,
        package=PACKAGE,
        pypi_version="1.0.0",
        npm_version=None,
        feeds=feeds,
    )


def test_empty_atom_does_not_call_releases_api_offline(monkeypatch):
    def boom(url: str):
        raise AssertionError(url)

    monkeypatch.setattr("conduit.feeds.http.cached_get", boom)
    hits = GitHubReleasesSource().collect(_target(), FeedClient(allow_network=False))
    assert hits == []


def test_empty_atom_uses_releases_api_when_live(monkeypatch):
    def fake(url: str):
        if url.endswith("/releases"):
            return (
                json.dumps(
                    [
                        {
                            "tag_name": "v1.2.0",
                            "name": "one",
                            "body": "limit renamed to page_size",
                            "html_url": "https://example.com/r",
                        }
                    ]
                ),
                "",
            )
        return None, "HTTP 404"

    monkeypatch.setattr("conduit.feeds.http.cached_get", fake)
    hits = GitHubReleasesSource().collect(_target(), FeedClient(allow_network=True))
    assert any(hit.change == "version" and hit.to_version == "1.2.0" for hit in hits)
    assert any(hit.change == "param_rename" for hit in hits)


def test_cached_get_sends_github_token_and_caches(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _Response:
        def __init__(self, status: int, text: str, headers: dict | None = None):
            self.status_code = status
            self.text = text
            self.headers = headers or {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            calls.append((url, dict(headers or {})))
            return _Response(200, "body")

    monkeypatch.setenv("GITHUB_TOKEN", "test-token-value")
    monkeypatch.setattr(httpx, "Client", _Client)
    api = f"https://api.github.com/repos/{REPO}/releases"
    pypi = PYPI
    assert cached_get(api) == ("body", "")
    assert cached_get(api) == ("body", "")
    assert cached_get(pypi) == ("body", "")
    assert len(calls) == 2
    assert calls[0][1]["Authorization"] == "Bearer test-token-value"
    assert calls[0][1]["Accept"] == "application/vnd.github+json"
    assert "Authorization" not in calls[1][1]


def test_cached_get_retries_rate_limit_once_and_does_not_cache_timeouts(monkeypatch):
    script = [
        (429, "", {"Retry-After": "9"}),
        (200, "ok", {}),
    ]
    slept: list[float] = []

    class _Response:
        def __init__(self, status: int, text: str, headers: dict | None = None):
            self.status_code = status
            self.text = text
            self.headers = headers or {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            status, text, hdrs = script.pop(0)
            return _Response(status, text, hdrs)

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr("conduit.feeds.http.time.sleep", lambda delay: slept.append(delay))
    assert cached_get("https://example.com/limited") == ("ok", "")
    assert slept == [2.0]

    class _TimeoutClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "Client", _TimeoutClient)
    assert cached_get("https://example.com/slow") == (None, "timed out")
    assert cached_get("https://example.com/slow") == (None, "timed out")
