"""Offline pydantic validator-hop smoke: CLI watch → apply → watch without LLM keys."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.packet.validate import validate_packet

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-validator-fixture"
PACKET_PATH = REPO / "examples" / "sample-packet" / "pydantic-validator-hop.json"


def _packet() -> dict:
    return json.loads(PACKET_PATH.read_text(encoding="utf-8"))


def _copy_fixture(dest: Path) -> Path:
    shutil.copytree(
        FIXTURE,
        dest,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", ".conduit"),
    )
    return dest


def _residue_counts(root: Path) -> tuple[int, int]:
    text = (root / "src" / "model.py").read_text(encoding="utf-8")
    return text.count(".dict("), text.count("class Config")


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


def test_pydantic_validator_hop_packet_validates():
    data = _packet()
    assert validate_packet(data) == []
    types = {r.get("type") for r in data["rules"]}
    assert "DEPENDENCY_BUMP" in types
    assert "AST_DECLARATION_REWRITE" in types
    assert "AST_CALL_REWRITE" in types
    effects = data.get("side_effects") or []
    joined = " ".join(str(e.get("detail") or "") for e in effects)
    assert "classmethod" in joined.lower() or "signature" in joined.lower()
    assert any(
        str(r.get("old_callee") or "").endswith(".dict")
        or str(r.get("old_callee") or "") == "dict"
        for r in data["rules"]
        if isinstance(r, dict)
    )


def test_pydantic_validator_smoke_cli_watch_apply_watch(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    _stub_sync(monkeypatch)
    tree = _copy_fixture(tmp_path / "fixture")
    src = tree / "src" / "model.py"
    # Seed pin at to_version so first Watch is bump_dirty (exit 1), not pre_bump.
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    runner = CliRunner()
    packet_arg = str(PACKET_PATH)

    dirty = runner.invoke(
        app, ["watch", "--path", str(tree), "--packet", packet_arg, "--json"]
    )
    assert dirty.exit_code == 1, dirty.output
    dirty_payload = json.loads(dirty.stdout)
    assert dirty_payload["status"] == "bump_dirty"
    assert dirty_payload["exit_code"] == 1
    assert any("validator" in item for item in dirty_payload["leftovers"])

    applied = runner.invoke(app, ["apply", "--path", str(tree), "--packet", packet_arg])
    assert applied.exit_code == 0, applied.output
    after = src.read_text(encoding="utf-8")
    assert '@field_validator("name")' in after
    assert "field_field_validator" not in after
    assert "from pydantic import BaseModel, field_validator" in after
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
    clean_payload = json.loads(clean.stdout)
    assert clean_payload["status"] == "clean"
    assert clean_payload["exit_code"] == 0
    assert clean_payload["leftovers"] == []

    dict_hits, config_hits = _residue_counts(tree)
    assert dict_hits == 0
    assert config_hits == 0
    body = src.read_text(encoding="utf-8")
    assert "model_dump" in body
    assert "model_config = ConfigDict(from_attributes=True)" in body.replace(
        " ", ""
    ) or "from_attributes=True" in body
