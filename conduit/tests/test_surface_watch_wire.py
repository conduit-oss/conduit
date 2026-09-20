"""PR2: Watch/apply fail-closed on binder completeness."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.watch import evaluate_watch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-validator-fixture"


def _dict_only_packet() -> dict:
    return {
        "packet_id": "pydantic-dict-miss-watch",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.10.13",
        "to_version": "2.0.0",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "pydantic",
                "from_version": "1.10.13",
                "to_version": "2.0.0",
                "ecosystems": ["pip"],
                "reason": "pin",
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "BaseModel.dict",
                "new_callee": "model_dump",
                "reason": "docs-shaped; consumers write obj.dict()",
            },
        ],
    }


def _copy_fixture(dest: Path) -> Path:
    shutil.copytree(
        FIXTURE,
        dest,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", ".conduit"),
    )
    return dest


def test_watch_basemodel_dict_packet_is_unverified_not_clean(tmp_path: Path):
    tree = _copy_fixture(tmp_path / "fixture")
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    verdict = evaluate_watch(root=tree, packet=_dict_only_packet())
    assert verdict.exit_code == 1
    assert verdict.status == "unverified"
    assert verdict.binding is not None
    assert any(
        o.chain.endswith(".dict") or o.chain == "self.dict"
        for o in verdict.binding.observations
    )
    payload = verdict.to_dict()
    assert payload["completeness"]["status"] == "unverified"
    assert payload["completeness"]["observation_count"] >= 1


def test_watch_cli_json_includes_completeness(tmp_path: Path):
    tree = _copy_fixture(tmp_path / "fixture")
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    packet_path = tmp_path / "dict-only.json"
    packet_path.write_text(json.dumps(_dict_only_packet()), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["watch", "--path", str(tree), "--packet", str(packet_path), "--json"],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "unverified"
    assert "completeness" in payload
    assert any(
        "dict" in (o.get("chain") or "")
        for o in payload["completeness"]["observations"]
    )


def test_watch_validator_hop_clean_keeps_unverified_completeness_without_fail(
    tmp_path: Path, monkeypatch
):
    """After validator hop apply, gate is clean; completeness may be unverified w/ 0 obs."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "conduit.patcher.sync_env.sync_bumped_packages",
        lambda *args, **kwargs: [],
    )
    tree = _copy_fixture(tmp_path / "fixture")
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    hop = REPO / "examples" / "sample-packet" / "pydantic-validator-hop.json"
    runner = CliRunner()
    applied = runner.invoke(app, ["apply", "--path", str(tree), "--packet", str(hop)])
    assert applied.exit_code == 0, applied.output
    clean = runner.invoke(
        app, ["watch", "--path", str(tree), "--packet", str(hop), "--json"]
    )
    assert clean.exit_code == 0, clean.output
    payload = json.loads(clean.stdout)
    assert payload["status"] == "clean"
    assert payload["completeness"]["status"] == "unverified"
    assert payload["completeness"]["observation_count"] == 0
