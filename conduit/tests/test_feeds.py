"""Offline feed watch: recorded fixtures, no live network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conduit.feeds.catalog import FEEDS
from conduit.feeds.client import FeedClient, FeedOfflineError
from conduit.feeds.model import FeedHit
from conduit.feeds.packet import packets_from_hits
from conduit.feeds.sources import default_sources
from conduit.feeds.sources.changelog import parse_changelog
from conduit.feeds.sources.github_releases import parse_github_atom
from conduit.feeds.sources.npm import parse_npm_json
from conduit.feeds.sources.openapi import diff_openapi
from conduit.feeds.sources.pypi import parse_pypi_json
from conduit.feeds.targets import WatchTarget
from conduit.feeds.watch import watch_outside
from conduit.main import app
from conduit.packet.validate import validate_packet

REPO = Path(__file__).resolve().parents[2]
FEEDS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
FIXTURE = REPO / "examples" / "wrapped-openai-fixture"


def _client() -> FeedClient:
    return FeedClient(root=FEEDS_DIR, allow_network=False)


def test_offline_client_does_not_touch_the_network(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("live network")

    monkeypatch.setattr("httpx.get", boom)
    monkeypatch.setattr("httpx.Client", boom)
    client = _client()
    text = client.get_text("https://pypi.org/pypi/openai/json")
    assert "1.0.0" in text
    with pytest.raises(FeedOfflineError):
        client.get_text("https://example.invalid/missing")


def test_github_atom_skips_prerelease_and_reads_renames():
    text = (FEEDS_DIR / "github" / "openai-python-releases.atom").read_text(encoding="utf-8")
    hits = parse_github_atom(
        text,
        url="https://github.com/openai/openai-python/releases.atom",
        package="openai",
        installed="0.28.1",
    )
    version = next(hit for hit in hits if hit.change == "version")
    assert version.to_version == "1.0.0"
    assert version.confidence == "high"
    call = next(hit for hit in hits if hit.change == "call_rename")
    assert call.old == "openai.ChatCompletion.create"
    assert call.new == "openai.chat.completions.create"
    assert call.confidence == "medium"
    assert not parse_github_atom(
        text,
        url="https://github.com/openai/openai-python/releases.atom",
        package="openai",
        installed="1.0.0",
    )


def test_pypi_reports_newer_stable_only():
    text = (FEEDS_DIR / "pypi" / "openai.json").read_text(encoding="utf-8")
    hits = parse_pypi_json(text, url="https://pypi.org/pypi/openai/json", package="openai", installed="0.28.1")
    assert len(hits) == 1
    assert hits[0].to_version == "1.0.0"
    assert hits[0].confidence == "high"
    assert parse_pypi_json(
        text, url="https://pypi.org/pypi/openai/json", package="openai", installed="1.0.0"
    ) == []


def test_npm_version_check_when_a_pin_exists():
    text = (FEEDS_DIR / "npm" / "openai.json").read_text(encoding="utf-8")
    hits = parse_npm_json(
        text, url="https://registry.npmjs.org/openai", package="openai", installed="3.3.0"
    )
    assert hits[0].ecosystem == "npm"
    assert hits[0].to_version == "4.0.0"
    assert hits[0].confidence == "high"


def test_openapi_pairs_renamed_path_and_ignores_pure_additions():
    previous = (FEEDS_DIR / "openapi" / "openai-previous.yaml").read_text(encoding="utf-8")
    latest = (FEEDS_DIR / "openapi" / "openai-latest.yaml").read_text(encoding="utf-8")
    hits = diff_openapi(
        previous,
        latest,
        package="openai",
        previous_url="prev",
        latest_url="latest",
    )
    renamed = next(hit for hit in hits if hit.change == "path_rename")
    assert renamed.old == "POST /v1/completions"
    assert renamed.new == "POST /v1/chat/completions"
    assert renamed.confidence == "medium"
    assert any(hit.change == "removed" and "engines" in hit.old for hit in hits)
    assert not any("/v1/files" in hit.new or "/v1/files" in hit.old for hit in hits if hit.change != "removed")
    assert any(hit.change == "param_rename" and hit.old == "max_tokens" for hit in hits)


def test_changelog_is_lowest_confidence():
    text = (FEEDS_DIR / "changelog" / "openai-deprecations.html").read_text(encoding="utf-8")
    hits = parse_changelog(text, url="https://platform.openai.com/docs/deprecations", package="openai", ecosystem="pypi")
    assert hits
    assert {hit.confidence for hit in hits} == {"low"}
    assert any(hit.old == "openai.ChatCompletion.create" for hit in hits)


def test_default_sources_are_replaceable():
    names = [source.name for source in default_sources()]
    assert names[:5] == ["github_releases", "pypi", "npm", "openapi", "changelog"]

    class OnlyVersion:
        name = "custom"
        confidence = "high"

        def collect(self, target, client):
            return [
                FeedHit(
                    source="custom",
                    confidence="high",
                    package=target.package,
                    ecosystem="pypi",
                    from_version="0.28.1",
                    to_version="1.0.0",
                    change="version",
                    old="0.28.1",
                    new="1.0.0",
                    detail="custom feed",
                    url="https://example.com/custom",
                )
            ]

    result = watch_outside(FIXTURE, client=_client(), sources=[OnlyVersion()])
    assert result.services == ["openai"]
    assert len(result.packets) == 1
    assert result.packets[0]["rules"][0]["type"] == "DEPENDENCY_BUMP"
    assert "github_releases" not in json.dumps(result.packets)


def test_watch_outside_skips_internal_modules(tmp_path: Path):
    (tmp_path / "app.py").write_text("import pydantic\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\n", encoding="utf-8")
    result = watch_outside(tmp_path, client=_client(), service="pydantic")
    assert result.packets == []
    assert result.services == []
    assert any("not an outside service" in item for item in result.skipped)


def test_fixture_watch_merges_a_schema_valid_packet():
    result = watch_outside(FIXTURE, client=_client())
    assert result.warnings == []
    assert len(result.packets) == 1
    packet = result.packets[0]
    assert validate_packet(packet) == []
    assert packet["from_version"] == "0.28.1"
    assert packet["to_version"] == "1.0.0"
    call = next(
        rule
        for rule in packet["rules"]
        if rule["type"] == "AST_CALL_REWRITE" and rule["old_callee"] == "openai.ChatCompletion.create"
    )
    assert call["new_callee"] == "openai.chat.completions.create"
    assert "medium" in call["reason"]
    assert "low" in packet["notes"]
    assert "changelog" in packet["notes"]
    assert any(effect["old_shape"] == "POST /v1/completions" for effect in packet["side_effects"])
    bare = [
        rule
        for rule in packet["rules"]
        if rule.get("old_callee") == "ChatCompletion.create"
    ]
    assert bare


def test_watch_cli_prints_packet_from_fixture():
    result = CliRunner().invoke(
        app,
        [
            "watch",
            "--path",
            str(FIXTURE),
            "--feeds",
            str(FEEDS_DIR),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["services"] == ["openai"]
    assert payload["packets"][0]["package"] == "openai"
    assert "ChatCompletion.create" in json.dumps(payload["packets"])


def test_packets_from_hits_keeps_highest_confidence_rule():
    low = FeedHit(
        source="changelog",
        confidence="low",
        package="openai",
        ecosystem="pypi",
        from_version="0.28.1",
        to_version="1.0.0",
        change="call_rename",
        old="openai.ChatCompletion.create",
        new="openai.chat.completions.create",
        detail="page",
        url="https://example.com/changelog",
    )
    medium = FeedHit(
        source="github_releases",
        confidence="medium",
        package="openai",
        ecosystem="pypi",
        from_version="0.28.1",
        to_version="1.0.0",
        change="call_rename",
        old="openai.ChatCompletion.create",
        new="openai.chat.completions.create",
        detail="notes",
        url="https://example.com/release",
    )
    packets, problems = packets_from_hits([low, medium])
    assert problems == []
    rule = next(item for item in packets[0]["rules"] if item["type"] == "AST_CALL_REWRITE" and "openai.Chat" in item["old_callee"])
    assert rule["reason"].startswith("github_releases (medium)")
    assert FEEDS["openai"].github_repo == "openai/openai-python"
