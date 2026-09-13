"""Staged apply: SDK rules before REST literal rules."""

from __future__ import annotations

from pathlib import Path

from conduit.patcher.engine import apply_packet
from conduit.patcher.rule_stages import partition_rules


def test_partition_rules_sdk_vs_rest():
    rules = [
        {"type": "DEPENDENCY_BUMP", "package": "openai"},
        {"type": "AST_CALL_REWRITE", "old_callee": "a", "new_callee": "b"},
        {"type": "EXACT_STRING_REPLACE", "match": "/v1/x", "replace": "/v1/y"},
        {"type": "KEY_RENAME", "old_key": "a", "new_key": "b"},
        {"type": "MYSTERY_RULE"},
    ]
    sdk, rest, post, unknown = partition_rules(rules)
    assert len(sdk) == 2
    assert len(rest) == 2
    assert not post
    assert unknown


def test_staged_apply_sdk_before_rest(tmp_path: Path):
    src = (
        'URL = "/v1/completions"\n'
        "def run():\n"
        "    return openai.Completion.create(model='gpt-4', prompt='hi')\n"
    )
    path = tmp_path / "app.py"
    path.write_text(src, encoding="utf-8")

    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "/v1/completions",
                "replace": "/v1/chat/completions",
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "openai.Completion.create",
                "new_callee": "openai.chat.completions.create",
            },
        ],
    }

    report = apply_packet(tmp_path, packet, dry_run=False, require_context=False)
    text = path.read_text(encoding="utf-8")
    assert "openai.chat.completions.create" in text
    assert "/v1/chat/completions" in text
    assert "openai.Completion.create" not in text
    assert "/v1/completions" not in text
    assert report.stage_counts.get("sdk") == 1
    assert report.stage_counts.get("rest") == 1


def test_apply_skips_invalid_ast_call_rewrite(tmp_path: Path):
    path = tmp_path / "app.py"
    path.write_text(
        "def run():\n    return openai.FineTune.list()\n",
        encoding="utf-8",
    )
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "openai.FineTune.list",
                "new_callee": "/v1/fine-tunes",
            },
        ],
    }
    report = apply_packet(tmp_path, packet, dry_run=False, require_context=False)
    assert "openai.FineTune.list()" in path.read_text(encoding="utf-8")
    assert any("skipped unsafe AST_CALL_REWRITE" in s for s in report.skips)


def test_apply_stages_kwarg_rest_only(tmp_path: Path):
    path = tmp_path / "app.py"
    path.write_text('x = "/v1/engines"\n', encoding="utf-8")
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "/v1/engines",
                "replace": "/v1/models",
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "Engine.list",
                "new_callee": "models.list",
            },
        ],
    }
    report = apply_packet(
        tmp_path, packet, dry_run=False, require_context=False, stages="rest"
    )
    assert "/v1/models" in path.read_text(encoding="utf-8")
    assert report.stage_counts.get("rest") == 1
    assert "sdk" not in report.stage_counts
