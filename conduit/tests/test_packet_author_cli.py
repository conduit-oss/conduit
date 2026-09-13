"""Guided packet CLI (new / diff-rules / test) + demo credential gate."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from typer.testing import CliRunner

from conduit.credentials import ensure_verify_credentials
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


def _tiny_packet(
    *,
    to_version: str,
    rules: list[dict],
    from_version: str = "0",
) -> dict:
    return {
        "packet_id": f"demo-pkg-pypi-{to_version}",
        "package": "demo-pkg",
        "ecosystem": "pypi",
        "from_version": from_version,
        "to_version": to_version,
        "sources": [],
        "notes": "fixture",
        "rules": rules,
    }


def test_credentials_demo_skips_consumer_openai_key(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text(
        "OPENAI_API_KEY = True\n", encoding="utf-8"
    )
    # Must not raise when demo=True even without a consumer key.
    ensure_verify_credentials(
        tmp_path,
        {"package": "openai"},
        want_llm=False,
        interactive=False,
        demo=True,
    )


def test_py_typed_marker_present():
    root = Path(__file__).resolve().parents[1] / "src" / "conduit" / "py.typed"
    assert root.is_file()
    packaged = resources.files("conduit").joinpath("py.typed")
    assert packaged.is_file()


def test_create_packet_new_scaffold_only(tmp_path: Path):
    out = tmp_path / "demo-pkg-pypi-1.0.0.json"
    path, packet, _warnings = create_packet_new(
        package="demo-pkg",
        ecosystem="pypi",
        from_version="0.1.0",
        to_version="1.0.0",
        out=out,
        prefer_detect=False,
    )
    assert path == out.resolve()
    assert out.is_file()
    assert packet["package"] == "demo-pkg"
    assert packet["from_version"] == "0.1.0"
    assert packet["to_version"] == "1.0.0"
    assert validate_packet(packet) == []


def test_cli_packet_new_scaffold_only(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
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
            "--from",
            "1.0.0",
            "--to",
            "2.0.0",
            "--scaffold-only",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file(), result.output
    assert "Try it:" in result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["package"] == "widgets"
    assert data["to_version"] == "2.0.0"
    assert validate_packet(data) == []


def test_diff_packet_rules_unit():
    prev = _tiny_packet(
        to_version="1.0.0",
        rules=[
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "old-a",
                "replace": "new-a",
                "target_files": ["*.py"],
            }
        ],
    )
    cur = _tiny_packet(
        from_version="1.0.0",
        to_version="2.0.0",
        rules=[
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "old-b",
                "replace": "new-b",
                "target_files": ["*.py"],
            },
            {
                "type": "DEPENDENCY_BUMP",
                "package": "demo-pkg",
                "from_version": "1.0.0",
                "to_version": "2.0.0",
                "ecosystems": ["pip"],
            },
        ],
    )
    diff = diff_packet_rules(cur, prev)
    assert len(diff["added"]) == 2
    assert len(diff["removed"]) == 1
    assert "EXACT_STRING_REPLACE" in summarize_rule(diff["removed"][0])


def test_cli_packet_diff_rules(tmp_path: Path):
    prev = tmp_path / "demo-pkg-pypi-1.0.0.json"
    cur = tmp_path / "demo-pkg-pypi-2.0.0.json"
    prev.write_text(
        json.dumps(
            _tiny_packet(
                to_version="1.0.0",
                rules=[
                    {
                        "type": "EXACT_STRING_REPLACE",
                        "match": "v1",
                        "replace": "v2",
                        "target_files": ["*.py"],
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    cur.write_text(
        json.dumps(
            _tiny_packet(
                from_version="1.0.0",
                to_version="2.0.0",
                rules=[
                    {
                        "type": "EXACT_STRING_REPLACE",
                        "match": "v2",
                        "replace": "v3",
                        "target_files": ["*.py"],
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["packet", "diff-rules", str(cur), "--previous", str(prev)],
    )
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    assert "Removed" in result.output
    assert "v2" in result.output or "EXACT_STRING_REPLACE" in result.output


def test_cli_packet_test_dry_run(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text(
        "import openai\nmodel = 'gpt-4-0613'\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    pkt = tmp_path / "packet.json"
    pkt.write_text(
        json.dumps(
            {
                "packet_id": "openai-0.28.1-1.0.0",
                "package": "openai",
                "ecosystem": "pypi",
                "from_version": "0.28.1",
                "to_version": "1.0.0",
                "sources": [],
                "notes": "test",
                "rules": [
                    {
                        "type": "EXACT_STRING_REPLACE",
                        "match": "gpt-4-0613",
                        "replace": "gpt-4o",
                        "target_files": ["*.py"],
                    },
                    {
                        "type": "DEPENDENCY_BUMP",
                        "package": "openai",
                        "from_version": "0.28.1",
                        "to_version": "1.0.0",
                        "ecosystems": ["pip"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["packet", "test", "--packet", str(pkt), "--path", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Packet is valid" in result.output
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

    monkeypatch.setattr(
        "conduit.llm.get_llm_client", lambda **kwargs: FakeClient()
    )
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
