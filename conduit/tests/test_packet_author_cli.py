"""Tests for conduit packet new / packet test authoring from links."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from conduit.main import app
from conduit.packet.author import (
    create_packet_new,
    default_packet_out_path,
    format_packet_summary,
    guess_source_kind,
    packet_id_for_target,
)
from conduit.packet.synthesize import (
    empty_packet,
    normalize_side_effect_kind,
    normalize_source_kind,
    synthesize_from_evidence,
)
from conduit.packet.validate import validate_packet


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)
    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: None)
    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: None)


def test_guess_source_kind():
    assert guess_source_kind("https://example.com/CHANGELOG.md") == "changelog"
    assert (
        guess_source_kind("https://github.com/acme/sdk/releases/tag/v1")
        == "github_release"
    )
    assert guess_source_kind("https://docs.example.com/migrate") == "docs"


def test_default_out_path_and_id():
    assert packet_id_for_target("google-genai", "pypi", "1.0.0") == (
        "google-genai-pypi-1.0.0"
    )
    path = default_packet_out_path(
        package="google-genai", ecosystem="pypi", version="1.0.0"
    )
    assert path == Path("packets/google-genai-pypi-1.0.0.json")


def test_create_packet_new_no_llm_with_sources(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)

    def fake_fetch(url: str, *, timeout: float = 30.0) -> str:
        return f"# Migration\nUse new APIs from {url}\n"

    monkeypatch.setattr("conduit.packet.author.fetch_url", fake_fetch)

    out = tmp_path / "packets" / "google-genai-pypi-1.2.0.json"
    path, packet, warnings = create_packet_new(
        package="google-genai",
        ecosystem="pypi",
        version="1.2.0",
        source_urls=[
            "https://googleapis.github.io/python-genai/migrate.html",
            "https://example.com/CHANGELOG.md",
        ],
        out=out,
        enrich=True,
    )
    assert path == out
    assert out.is_file()
    assert packet["packet_id"] == "google-genai-pypi-1.2.0"
    assert packet["from_version"] == "*"
    assert packet["to_version"] == "1.2.0"
    assert len(packet["sources"]) == 2
    assert validate_packet(packet) == []
    rules = packet["rules"]
    assert len(rules) == 1
    assert rules[0]["type"] == "DEPENDENCY_BUMP"
    assert rules[0]["to_version"] == "1.2.0"
    assert not any(
        r.get("type")
        in {
            "EXACT_STRING_REPLACE",
            "REGEX_REPLACE",
            "AST_CALL_REWRITE",
            "AST_IMPORT_REWRITE",
            "AST_ATTR_RENAME",
            "AST_PARAM_RENAME",
        }
        for r in rules
    )
    assert any("LLM not configured" in w for w in warnings)


def test_create_packet_new_scaffold_only_skips_llm(tmp_path: Path, monkeypatch):
    class Boom:
        def complete_json(self, *args, **kwargs):
            raise AssertionError("LLM should not be called")

        def run_agent(self, *args, **kwargs):
            raise AssertionError("LLM should not be called")

    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: Boom())
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "docs body",
    )

    out = tmp_path / "widgets-pypi-2.0.0.json"
    _path, packet, warnings = create_packet_new(
        package="widgets",
        ecosystem="pypi",
        version="2.0.0",
        source_urls=["https://docs.example.com/migrate"],
        out=out,
        scaffold_only=True,
        enrich=False,
    )
    assert packet["rules"][0]["type"] == "DEPENDENCY_BUMP"
    assert len(packet["rules"]) == 1
    assert any("Enrichment skipped" in w for w in warnings)
    assert validate_packet(packet) == []


def test_create_packet_new_enrich_merges_rules_and_side_effects(
    tmp_path: Path, monkeypatch
):
    class FakeClient:
        def complete_json(self, *args, **kwargs):
            return {
                "packet_id": "ignored",
                "package": "widgets",
                "ecosystem": "pypi",
                "from_version": "*",
                "to_version": "2.0.0",
                "sources": [],
                "notes": "from docs",
                "side_effects": [
                    {
                        "kind": "config",
                        "detail": "Update env var names for the new client.",
                    }
                ],
                "rules": [
                    {
                        "type": "AST_IMPORT_REWRITE",
                        "target_files": ["*.py"],
                        "old_import": "old_sdk",
                        "new_import": "widgets",
                        "reason": "docs say rename import",
                    }
                ],
            }

        def run_agent(self, *args, **kwargs):
            return {
                "notes": "evidence note",
                "sources": [],
                "side_effects": [
                    {
                        "kind": "other",
                        "detail": "Multi-statement client construction must be hand-edited.",
                    }
                ],
                "rules": [
                    {
                        "type": "AST_CALL_REWRITE",
                        "target_files": ["*.py"],
                        "old_callee": "old_sdk.Client",
                        "new_callee": "widgets.Client",
                        "reason": "docs",
                    }
                ],
            }

    monkeypatch.setattr(
        "conduit.packet.author.get_llm_client", lambda **kwargs: FakeClient()
    )
    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: FakeClient())
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "migration guide body with Client rename",
    )

    out = tmp_path / "widgets-pypi-2.0.0.json"
    _path, packet, _warnings = create_packet_new(
        package="widgets",
        ecosystem="pypi",
        version="2.0.0",
        source_urls=["https://docs.example.com/migrate"],
        out=out,
        enrich=True,
    )
    assert validate_packet(packet) == []
    types = {r.get("type") for r in packet["rules"]}
    assert "DEPENDENCY_BUMP" in types
    assert "AST_IMPORT_REWRITE" in types or "AST_CALL_REWRITE" in types
    effects = packet.get("side_effects") or []
    assert effects
    assert all("kind" in e and "detail" in e for e in effects)


def test_cli_packet_new_scaffold_only(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "body",
    )
    out = tmp_path / "widgets-pypi-2.0.0.json"
    result = CliRunner().invoke(
        app,
        [
            "packet",
            "new",
            "--package",
            "widgets",
            "--ecosystem",
            "pypi",
            "--version",
            "2.0.0",
            "--source-url",
            "https://docs.example.com/migrate",
            "--scaffold-only",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file(), result.output
    assert "Try it:" in result.output
    assert "From version" not in result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["package"] == "widgets"
    assert data["to_version"] == "2.0.0"
    assert data["from_version"] == "*"
    assert validate_packet(data) == []


def test_cli_packet_new_missing_flags_non_tty(monkeypatch):
    _disable_llm(monkeypatch)
    result = CliRunner().invoke(app, ["packet", "new", "--package", "widgets"])
    assert result.exit_code != 0


def test_cli_packet_new_help_has_no_from():
    # Wide + no color so Rich does not wrap/ANSI-split option names.
    result = CliRunner(env={"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}).invoke(
        app, ["packet", "new", "--help"]
    )
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    plain = re.sub(r"\s+", " ", plain)
    assert "--from-consumer" not in plain
    assert "From version" not in plain
    # Exact option --from should not appear (allow --from in prose only if any)
    assert re.search(r"--from\b", plain) is None
    assert "--source-url" in plain
    assert "--version" in plain or "--to" in plain


def test_packet_test_validate_only(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "body",
    )
    out = tmp_path / "widgets-pypi-1.0.0.json"
    create_packet_new(
        package="widgets",
        ecosystem="pypi",
        version="1.0.0",
        source_urls=["https://example.com/docs"],
        out=out,
        scaffold_only=True,
        enrich=False,
    )
    result = CliRunner().invoke(app, ["packet", "test", "--packet", str(out)])
    assert result.exit_code == 0, result.output
    assert "Packet is valid" in result.output
    assert "packet_id:" in result.output
    assert "packet test OK" in result.output


def test_packet_test_with_path_dry_run(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "requirements.txt").write_text("widgets==0.1.0\n", encoding="utf-8")
    (consumer / "app.py").write_text("print('hi')\n", encoding="utf-8")

    packet_path = tmp_path / "widgets-pypi-1.0.0.json"
    create_packet_new(
        package="widgets",
        ecosystem="pypi",
        version="1.0.0",
        out=packet_path,
        scaffold_only=True,
        enrich=False,
    )
    result = CliRunner().invoke(
        app,
        ["packet", "test", "--packet", str(packet_path), "--path", str(consumer)],
    )
    assert result.exit_code == 0, result.output
    assert "Dry-run apply" in result.output
    assert "packet test OK" in result.output


def test_format_packet_summary_includes_side_effects():
    packet: dict[str, Any] = {
        "packet_id": "x-pypi-1",
        "package": "x",
        "ecosystem": "pypi",
        "from_version": "*",
        "to_version": "1",
        "sources": [{"url": "https://example.com", "kind": "docs"}],
        "side_effects": [{"kind": "webhook", "detail": "Update payload field"}],
        "rules": [],
        "notes": "n",
    }
    text = format_packet_summary(packet)
    assert "side_effect[webhook]" in text
    assert "source[docs]" in text


def test_normalize_source_kind_aliases():
    assert normalize_source_kind("docs") == "docs"
    assert normalize_source_kind("documentation") == "docs"
    assert normalize_source_kind("Documentation") == "docs"
    assert normalize_source_kind("repository") == "other"
    assert normalize_source_kind("repo") == "other"
    assert normalize_source_kind("release") == "github_release"
    assert normalize_source_kind("github_release_notes") == "github_release"
    assert normalize_source_kind("swagger") == "openapi"
    assert normalize_source_kind("changes") == "changelog"
    assert normalize_source_kind("") == "other"
    assert normalize_source_kind("totally-unknown") == "other"


def test_normalize_side_effect_kind_aliases():
    assert normalize_side_effect_kind("db") == "database"
    assert normalize_side_effect_kind("env") == "config"
    assert normalize_side_effect_kind("configuration") == "config"
    assert normalize_side_effect_kind("webhook") == "webhook"


def _pin_only_llm_client():
    class FakeClient:
        def complete_json(self, *args, **kwargs):
            return {
                "packet_id": "ignored",
                "package": "widgets",
                "ecosystem": "pypi",
                "from_version": "*",
                "to_version": "2.0.0",
                "sources": [],
                "notes": "pin only",
                "side_effects": [],
                "rules": [
                    {
                        "type": "DEPENDENCY_BUMP",
                        "package": "widgets",
                        "from_version": "*",
                        "to_version": "2.0.0",
                        "ecosystems": ["pip", "pyproject"],
                        "reason": "pin only",
                    }
                ],
            }

        def run_agent(self, *args, **kwargs):
            return {
                "notes": "evidence pin only",
                "sources": [],
                "side_effects": [],
                "rules": [],
            }

    return FakeClient()


def test_create_packet_new_thin_enrich_refuses_write(tmp_path: Path, monkeypatch):
    from conduit.packet.author import ThinEnrichError

    client = _pin_only_llm_client()
    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "migration guide body with no rewrite hints",
    )
    out = tmp_path / "widgets-pypi-2.0.0.json"
    try:
        create_packet_new(
            package="widgets",
            ecosystem="pypi",
            version="2.0.0",
            source_urls=["https://docs.example.com/migrate"],
            out=out,
            enrich=True,
        )
        raise AssertionError("expected ThinEnrichError")
    except ThinEnrichError as exc:
        msg = str(exc)
        assert "zero call-site rules" in msg
        assert "https://docs.example.com/migrate" in msg
    assert not out.exists()


def test_create_packet_new_allow_pin_only_is_noisy(tmp_path: Path, monkeypatch):
    from conduit.packet.author import PIN_ONLY_MARKER

    client = _pin_only_llm_client()
    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "migration guide body",
    )
    out = tmp_path / "widgets-pypi-2.0.0.json"
    _path, packet, warnings = create_packet_new(
        package="widgets",
        ecosystem="pypi",
        version="2.0.0",
        source_urls=["https://docs.example.com/migrate"],
        out=out,
        enrich=True,
        allow_pin_only=True,
    )
    assert out.is_file()
    assert PIN_ONLY_MARKER in str(packet.get("notes") or "")
    assert any("not a rules-bearing hop" in w for w in warnings)
    assert validate_packet(packet) == []


def test_create_packet_new_allow_pin_only_without_marker_hard_fails(
    tmp_path: Path, monkeypatch
):
    from conduit.packet.author import PIN_ONLY_MARKER, ThinEnrichError

    client = _pin_only_llm_client()
    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "migration guide body",
    )

    def _broken_mark(packet, *, reason: str) -> None:
        packet["notes"] = "deliberately missing marker"

    monkeypatch.setattr("conduit.packet.author._mark_pin_only", _broken_mark)
    out = tmp_path / "widgets-pypi-2.0.0.json"
    try:
        create_packet_new(
            package="widgets",
            ecosystem="pypi",
            version="2.0.0",
            source_urls=["https://docs.example.com/migrate"],
            out=out,
            enrich=True,
            allow_pin_only=True,
        )
        raise AssertionError("expected ThinEnrichError")
    except ThinEnrichError as exc:
        assert PIN_ONLY_MARKER in str(exc)
    assert not out.exists()


def test_create_packet_new_scaffold_only_marks_pin_only(tmp_path: Path, monkeypatch):
    from conduit.packet.author import PIN_ONLY_MARKER

    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: object())
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "docs body",
    )
    out = tmp_path / "widgets-pypi-2.0.0.json"
    _path, packet, warnings = create_packet_new(
        package="widgets",
        ecosystem="pypi",
        version="2.0.0",
        source_urls=["https://docs.example.com/migrate"],
        out=out,
        scaffold_only=True,
        enrich=False,
    )
    assert packet["rules"][0]["type"] == "DEPENDENCY_BUMP"
    assert PIN_ONLY_MARKER in str(packet.get("notes") or "")
    assert any("not a rules-bearing hop" in w for w in warnings)
    assert any("Enrichment skipped" in w for w in warnings)
    assert validate_packet(packet) == []


def test_cli_packet_new_thin_enrich_exits_nonzero(tmp_path: Path, monkeypatch):
    client = _pin_only_llm_client()
    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "migration guide body",
    )
    out = tmp_path / "widgets-pypi-2.0.0.json"
    result = CliRunner().invoke(
        app,
        [
            "packet",
            "new",
            "--package",
            "widgets",
            "--ecosystem",
            "pypi",
            "--version",
            "2.0.0",
            "--source-url",
            "https://docs.example.com/migrate",
            "--out",
            str(out),
        ],
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert result.exit_code != 0, combined
    assert "zero call-site rules" in combined
    assert "https://docs.example.com/migrate" in combined
    assert not out.exists()


def test_cli_packet_new_allow_pin_only_warns_stderr(tmp_path: Path, monkeypatch):
    from conduit.packet.author import PIN_ONLY_MARKER

    client = _pin_only_llm_client()
    monkeypatch.setattr("conduit.packet.author.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: client)
    monkeypatch.setattr(
        "conduit.packet.author.fetch_url",
        lambda url, **kwargs: "migration guide body",
    )
    out = tmp_path / "widgets-pypi-2.0.0.json"
    result = CliRunner().invoke(
        app,
        [
            "packet",
            "new",
            "--package",
            "widgets",
            "--ecosystem",
            "pypi",
            "--version",
            "2.0.0",
            "--source-url",
            "https://docs.example.com/migrate",
            "--allow-pin-only",
            "--out",
            str(out),
        ],
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert result.exit_code == 0, combined
    assert "not a rules-bearing hop" in (result.stderr or combined)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert PIN_ONLY_MARKER in str(data.get("notes") or "")


def test_evidence_enrich_normalizes_synonym_source_kinds(monkeypatch):
    class FakeClient:
        def run_agent(self, *args, **kwargs):
            return {
                "notes": "ok",
                "sources": [
                    {
                        "url": "https://example.com/guide",
                        "kind": "documentation",
                    },
                    {
                        "url": "https://github.com/acme/sdk",
                        "kind": "repository",
                    },
                ],
                "side_effects": [
                    {"kind": "db", "detail": "Migrate stored option names."}
                ],
                "rules": [
                    {
                        "type": "AST_IMPORT_REWRITE",
                        "target_files": ["*.py"],
                        "old_import": "old_sdk",
                        "new_import": "widgets",
                        "reason": "docs",
                    }
                ],
            }

        def complete_json(self, *args, **kwargs):
            return self.run_agent()

    monkeypatch.setattr("conduit.llm.get_llm_client", lambda **kwargs: FakeClient())
    base = empty_packet(
        package="widgets",
        ecosystem="pypi",
        from_version="*",
        to_version="2.0.0",
        notes="seed",
    )
    base["rules"] = [
        {
            "type": "DEPENDENCY_BUMP",
            "package": "widgets",
            "from_version": "*",
            "to_version": "2.0.0",
            "ecosystems": ["pip", "pyproject"],
            "reason": "pin",
        }
    ]
    packet, warnings = synthesize_from_evidence(
        package="widgets",
        from_version="*",
        to_version="2.0.0",
        ecosystem="pypi",
        signals=[],
        base=base,
        seed_urls=["https://docs.example.com/migrate"],
        suggested_queries=["widgets migration"],
    )
    assert not any("failed validation" in w for w in warnings), warnings
    assert validate_packet(packet) == []
    kinds = {s["kind"] for s in packet["sources"] if isinstance(s, dict)}
    assert "docs" in kinds
    assert "other" in kinds
    assert "documentation" not in kinds
    assert "repository" not in kinds
    effects = packet.get("side_effects") or []
    assert effects
    assert effects[0]["kind"] == "database"


def test_normalize_llm_rule_aliases_match_schema():
    from conduit.packet.synthesize import normalize_llm_rules

    rules = normalize_llm_rules(
        [
            {
                "type": "AST_CALL_REWRITE",
                "scope": "pydantic.BaseModel",
                "old": "dict",
                "new": "model_dump",
                "arguments": {"x": 1},
                "reason": "rename",
            },
            {
                "type": "AST_ATTR_RENAME",
                "old": "__fields__",
                "new": "model_fields",
                "reason": "rename",
            },
            {
                "type": "AST_PARAM_RENAME",
                "scope": "Config",
                "old": "orm_mode",
                "new": "from_attributes",
                "reason": "rename",
            },
            {
                "type": "DEPENDENCY_BUMP",
                "package": "pydantic",
                "from_version": "*",
                "to_version": "2.0.0",
                "ecosystems": ["pip"],
                "reason": "pin",
            },
        ]
    )
    packet = {
        "packet_id": "pydantic-pypi-2.0.0",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "*",
        "to_version": "2.0.0",
        "rules": rules,
    }
    assert validate_packet(packet) == []
    assert rules[0]["old_callee"] == "dict"
    assert rules[0]["new_callee"] == "model_dump"
    assert rules[0]["target_files"] == ["*.py"]
    assert "arguments" not in rules[0]
    assert "scope" not in rules[0]
    assert rules[1]["old_attr"] == "__fields__"
    assert rules[2]["function_target"] == "Config"
    assert rules[2]["old_param"] == "orm_mode"
