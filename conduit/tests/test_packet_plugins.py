"""Tests for packet enrich plugins."""

from __future__ import annotations

from pathlib import Path

import pytest

from conduit.packet.author import create_packet_new
from conduit.packet.plugin import (
    BasePacketPlugin,
    EnrichGuide,
    ProposeResult,
    drop_uncited_enrich_rewrites,
    merge_propose_into_packet,
    stamp_rule_reason,
)
from conduit.packet.plugin_discovery import (
    PluginResolveError,
    resolve_plugin,
)
from conduit.packet.plugins.example_sdk import ExampleSdkPlugin
from conduit.packet.validate import validate_packet


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: None)


def test_stamp_rule_reason_once():
    rule = {"type": "DEPENDENCY_BUMP", "reason": "pin"}
    stamped = stamp_rule_reason(rule, "plugin:demo")
    assert stamped["reason"] == "[plugin:demo] pin"
    again = stamp_rule_reason(stamped, "plugin:demo")
    assert again["reason"] == "[plugin:demo] pin"


def test_merge_propose_stamps_plugin_reason():
    base = {
        "packet_id": "widgets-pypi-1.0.0",
        "package": "widgets",
        "ecosystem": "pypi",
        "from_version": "*",
        "to_version": "1.0.0",
        "sources": [],
        "notes": "base",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "widgets",
                "from_version": "*",
                "to_version": "1.0.0",
                "ecosystems": ["pip"],
                "reason": "pin",
            }
        ],
        "side_effects": [],
    }
    result = ProposeResult(
        rules=[
            {
                "type": "AST_ATTR_RENAME",
                "target_files": ["*.py"],
                "old_attr": "a.b",
                "new_attr": "a.c",
                "reason": "docs say so",
            }
        ],
        notes_append="from plugin",
    )
    merged = merge_propose_into_packet(base, result, plugin_name="widgets")
    assert any(
        r.get("type") == "AST_ATTR_RENAME"
        and str(r.get("reason", "")).startswith("[plugin:widgets]")
        for r in merged["rules"]
    )
    assert "from plugin" in merged["notes"]


def test_drop_uncited_enrich_rewrites():
    packet = {
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "x",
                "from_version": "*",
                "to_version": "1",
                "ecosystems": ["pip"],
                "reason": "pin",
            },
            {
                "type": "AST_ATTR_RENAME",
                "target_files": ["*.py"],
                "old_attr": "a.b",
                "new_attr": "a.c",
                "reason": "[plugin:demo] kept",
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "old.fn",
                "new_callee": "new.fn",
                "reason": "[enrich:research] no citation",
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "old.g",
                "new_callee": "new.g",
                "reason": "[enrich:research] see https://example.com/migrate",
            },
        ]
    }
    cleaned, dropped = drop_uncited_enrich_rewrites(packet)
    assert dropped == 1
    types = [r["type"] for r in cleaned["rules"]]
    assert types.count("AST_CALL_REWRITE") == 1
    assert "AST_ATTR_RENAME" in types


def test_resolve_plugin_none_and_explicit(monkeypatch):
    plug = ExampleSdkPlugin()
    monkeypatch.setattr(
        "conduit.packet.plugin_discovery.load_plugins",
        lambda names=None: [plug],
    )
    assert resolve_plugin(package="example-sdk", plugin="none") is None
    assert resolve_plugin(package="example-sdk", plugin="example-sdk") is plug
    assert resolve_plugin(package="example-sdk", plugin=None) is plug
    assert resolve_plugin(package="other", plugin=None) is None
    with pytest.raises(PluginResolveError):
        resolve_plugin(package="example-sdk", plugin="missing")


def test_resolve_plugin_ambiguous(monkeypatch):
    class A(BasePacketPlugin):
        name = "a"
        packages = ["shared"]

    class B(BasePacketPlugin):
        name = "b"
        packages = ["shared"]

    monkeypatch.setattr(
        "conduit.packet.plugin_discovery.load_plugins",
        lambda names=None: [A(), B()],
    )
    with pytest.raises(PluginResolveError, match="Multiple"):
        resolve_plugin(package="shared", plugin=None)


def test_create_packet_new_plugin_propose_scaffold_only(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "body",
    )
    plug = ExampleSdkPlugin()
    monkeypatch.setattr(
        "conduit.packet.plugin_discovery.resolve_plugin",
        lambda **kwargs: plug,
    )

    out = tmp_path / "example-sdk-pypi-2.0.0.json"
    _path, packet, warnings = create_packet_new(
        package="example-sdk",
        ecosystem="pypi",
        version="2.0.0",
        source_urls=["https://example.com/migrate"],
        out=out,
        scaffold_only=True,
        enrich=False,
    )
    assert validate_packet(packet) == []
    assert any(r.get("type") == "AST_ATTR_RENAME" for r in packet["rules"])
    assert any(
        str(r.get("reason", "")).startswith("[plugin:example-sdk]")
        for r in packet["rules"]
        if r.get("type") == "AST_ATTR_RENAME"
    )
    assert any("Using packet plugin" in w for w in warnings)
    assert any("Enrichment skipped" in w for w in warnings)


def test_create_packet_new_plugin_none_skips_propose(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "body",
    )
    called = {"propose": False}

    class Tracking(ExampleSdkPlugin):
        def propose(self, **kwargs):
            called["propose"] = True
            return super().propose(**kwargs)

    monkeypatch.setattr(
        "conduit.packet.plugin_discovery.load_plugins",
        lambda names=None: [Tracking()],
    )

    out = tmp_path / "example-sdk-pypi-2.0.0.json"
    _path, packet, _warnings = create_packet_new(
        package="example-sdk",
        ecosystem="pypi",
        version="2.0.0",
        source_urls=["https://example.com/migrate"],
        out=out,
        scaffold_only=True,
        plugin="none",
    )
    assert called["propose"] is False
    assert len(packet["rules"]) == 1
    assert packet["rules"][0]["type"] == "DEPENDENCY_BUMP"
