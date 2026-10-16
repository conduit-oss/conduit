"""Tests for apply allowlist expansion."""

from __future__ import annotations

from pathlib import Path

from conduit.prune.grep_imports import expand_allowlist_for_exact_rules


def test_expand_allowlist_adds_config_with_legacy_token(tmp_path: Path):
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "deployments.json").write_text(
        '{"prod-complete": "text-davinci-003"}',
        encoding="utf-8",
    )
    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "text-davinci-003",
                "replace": "gpt-5.6-terra",
            }
        ]
    }
    expanded = expand_allowlist_for_exact_rules(tmp_path, [], packet)
    rels = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in expanded}
    assert "configs/deployments.json" in rels


def test_expand_allowlist_skips_python_mentioning_model_id(tmp_path: Path):
    (tmp_path / "notes.py").write_text(
        'MODEL = "text-davinci-003"\n', encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENAI_MODEL=text-davinci-003\n", encoding="utf-8")
    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "text-davinci-003",
                "replace": "gpt-4o",
            }
        ]
    }
    expanded = expand_allowlist_for_exact_rules(tmp_path, [], packet)
    rels = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in expanded}
    assert ".env" in rels
    assert "notes.py" not in rels
