"""Offline smoke: hand-enriched live packet covers validator + Config on the fixture."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.packet.validate import validate_packet

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-validator-fixture"
LIVE_PACKET = REPO / "examples" / "sample-packet" / "pydantic-llm-mint-live.json"
PACKET_PATH = (
    REPO / "examples" / "sample-packet" / "pydantic-llm-mint-live-hand-enriched.json"
)
ENGINE_PATHS = (
    REPO / "conduit" / "src" / "conduit" / "patcher" / "engine.py",
    REPO / "conduit" / "src" / "conduit" / "watch.py",
    REPO / "conduit" / "src" / "conduit" / "patcher" / "leftovers.py",
    REPO / "conduit" / "src" / "conduit" / "patcher" / "ast_attr_call.py",
    REPO / "conduit" / "src" / "conduit" / "patcher" / "ast_import_rewrite.py",
    REPO / "conduit" / "src" / "conduit" / "patcher" / "string_replace.py",
)


def _packet() -> dict:
    return json.loads(PACKET_PATH.read_text(encoding="utf-8"))


def _copy_fixture(dest: Path) -> Path:
    shutil.copytree(
        FIXTURE,
        dest,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", ".conduit"),
    )
    return dest


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)


def _stub_sync(monkeypatch):
    monkeypatch.setattr(
        "conduit.patcher.sync_env.sync_bumped_packages",
        lambda *args, **kwargs: [],
    )


def _assert_fixture_v2(src_text: str) -> None:
    assert '@field_validator("name")' in src_text
    assert "field_field_validator" not in src_text
    assert "from pydantic import BaseModel, field_validator, ConfigDict" in src_text
    assert "class Config" not in src_text
    assert "orm_mode" not in src_text
    assert "model_config = ConfigDict(from_attributes=True)" in src_text
    assert ".dict(" not in src_text
    assert "model_dump" in src_text


def test_live_mint_receipt_not_hand_edited():
    """Sibling carries validator/Config; the frozen LLM mint is left as recorded."""
    live = json.loads(LIVE_PACKET.read_text(encoding="utf-8"))
    enriched = _packet()
    assert live["packet_id"] == "pydantic-pypi-2.0.0"
    assert enriched["packet_id"] == "pydantic-pypi-2.0.0-hand-enriched"
    live_calls = {
        r.get("old_callee")
        for r in live["rules"]
        if r.get("type") == "AST_CALL_REWRITE"
    }
    assert "validator" not in live_calls
    assert not any(r.get("type") == "EXACT_STRING_REPLACE" for r in live["rules"])
    assert "BaseModel.dict" in live_calls


def test_hand_enriched_packet_validates_and_declares_missing_families():
    data = _packet()
    assert validate_packet(data) == []
    assert data["packet_id"] == "pydantic-pypi-2.0.0-hand-enriched"
    types = {r.get("type") for r in data["rules"]}
    assert "DEPENDENCY_BUMP" in types
    assert "AST_IMPORT_REWRITE" in types
    assert "AST_CALL_REWRITE" in types
    assert "EXACT_STRING_REPLACE" in types

    call_olds = {
        r.get("old_callee")
        for r in data["rules"]
        if r.get("type") == "AST_CALL_REWRITE"
    }
    assert "validator" in call_olds
    assert "BaseModel.dict" in call_olds

    imports = [
        r
        for r in data["rules"]
        if r.get("type") == "AST_IMPORT_REWRITE"
    ]
    assert any(
        r.get("old_import") == "BaseModel, validator"
        and r.get("new_import") == "BaseModel, field_validator, ConfigDict"
        for r in imports
    )
    assert any(
        r.get("old_import") == "from pydantic import BaseSettings" for r in imports
    )

    config_rules = [
        r
        for r in data["rules"]
        if r.get("type") == "EXACT_STRING_REPLACE"
        and "class Config" in str(r.get("match") or "")
    ]
    assert len(config_rules) == 1
    assert "model_config = ConfigDict(from_attributes=True)" in config_rules[0]["replace"]

    validator = next(
        r
        for r in data["rules"]
        if r.get("type") == "AST_CALL_REWRITE" and r.get("old_callee") == "validator"
    )
    surface = validator["surface"]
    assert surface["export_path"] == ["validator"]
    assert "imported" in surface["spellings"]
    assert "decorator" in surface["use_kinds"]

    dict_rule = next(
        r
        for r in data["rules"]
        if r.get("type") == "AST_CALL_REWRITE" and r.get("old_callee") == "BaseModel.dict"
    )
    assert dict_rule["new_callee"] == "BaseModel.model_dump"
    assert dict_rule["surface"]["export_path"] == ["BaseModel", "dict"]


def test_apply_watch_engine_has_no_pydantic_vendor_branches():
    for path in ENGINE_PATHS:
        text = path.read_text(encoding="utf-8")
        assert "pydantic" not in text.lower(), path


def test_hand_enriched_apply_watch_from_fixture_pin(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    _stub_sync(monkeypatch)
    tree = _copy_fixture(tmp_path / "fixture")
    src = tree / "src" / "model.py"
    runner = CliRunner()
    packet_arg = str(PACKET_PATH)

    applied = runner.invoke(app, ["apply", "--path", str(tree), "--packet", packet_arg])
    assert applied.exit_code == 0, applied.output
    assert "packet enrichment" not in applied.output.lower()
    after = src.read_text(encoding="utf-8")
    _assert_fixture_v2(after)
    assert "pydantic==2.0.0" in (tree / "requirements.txt").read_text(encoding="utf-8")

    before_second = {
        "model": src.read_text(encoding="utf-8"),
        "req": (tree / "requirements.txt").read_text(encoding="utf-8"),
    }
    second = runner.invoke(app, ["apply", "--path", str(tree), "--packet", packet_arg])
    assert second.exit_code == 0, second.output
    assert src.read_text(encoding="utf-8") == before_second["model"]
    assert (tree / "requirements.txt").read_text(encoding="utf-8") == before_second["req"]
    assert "field_field_validator" not in src.read_text(encoding="utf-8")

    clean = runner.invoke(
        app, ["watch", "--path", str(tree), "--packet", packet_arg, "--json"]
    )
    assert clean.exit_code == 0, clean.output
    payload = json.loads(clean.stdout)
    assert payload["status"] == "clean"
    assert payload["exit_code"] == 0
    assert payload["leftovers"] == []
    assert payload["leftover_count"] == 0
    assert payload["completeness"]["status"] == "complete"


def test_hand_enriched_watch_dirty_then_clean_at_to_version(
    tmp_path: Path, monkeypatch
):
    _disable_llm(monkeypatch)
    _stub_sync(monkeypatch)
    tree = _copy_fixture(tmp_path / "fixture")
    src = tree / "src" / "model.py"
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    runner = CliRunner()
    packet_arg = str(PACKET_PATH)

    dirty = runner.invoke(
        app, ["watch", "--path", str(tree), "--packet", packet_arg, "--json"]
    )
    assert dirty.exit_code == 1, dirty.output
    dirty_payload = json.loads(dirty.stdout)
    assert dirty_payload["status"] in {"bump_dirty", "incomplete"}
    leftovers = dirty_payload.get("leftovers") or []
    completeness = dirty_payload.get("completeness") or {}
    validator_visible = any("validator" in item for item in leftovers) or any(
        (obs.get("chain") or "") == "validator"
        for obs in completeness.get("observations") or []
    )
    assert validator_visible

    applied = runner.invoke(app, ["apply", "--path", str(tree), "--packet", packet_arg])
    assert applied.exit_code == 0, applied.output
    _assert_fixture_v2(src.read_text(encoding="utf-8"))

    clean = runner.invoke(
        app, ["watch", "--path", str(tree), "--packet", packet_arg, "--json"]
    )
    assert clean.exit_code == 0, clean.output
    clean_payload = json.loads(clean.stdout)
    assert clean_payload["status"] == "clean"
    assert clean_payload["leftovers"] == []
    assert clean_payload["completeness"]["status"] == "complete"
