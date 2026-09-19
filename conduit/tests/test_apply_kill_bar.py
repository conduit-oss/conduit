"""Structural openai 0.28 → 1.x apply kill-bar regression."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conduit.packet.scope import filter_rules_to_source
from conduit.packet.validate import validate_packet
from conduit.patcher.engine import apply_packet
from conduit.patcher.openai_client_chain import apply_openai_client_chain
from conduit.prune.grep_imports import prune_by_imports

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "examples" / "demo-consumer"
SAMPLE_PACKET = REPO / "examples" / "sample-packet" / "conduit-packet.json"


def test_sample_packet_is_structural_kill_bar():
    data = json.loads(SAMPLE_PACKET.read_text(encoding="utf-8"))
    assert validate_packet(data) == []
    types = {r.get("type") for r in data["rules"]}
    assert "AST_CALL_REWRITE" in types
    assert "DEPENDENCY_BUMP" in types
    assert any(
        r.get("type") == "AST_CALL_REWRITE"
        and r.get("old_callee") == "openai.ChatCompletion.create"
        and r.get("new_callee") == "openai.chat.completions.create"
        for r in data["rules"]
    )


def test_demo_consumer_starts_as_openai_028():
    text = (DEMO / "src" / "ai_client.py").read_text(encoding="utf-8")
    assert "openai.ChatCompletion.create" in text
    assert "from openai import OpenAI" not in text
    assert "client.chat.completions.create" not in text
    req = (DEMO / "requirements.txt").read_text(encoding="utf-8")
    assert "openai==0.28.1" in req


def test_apply_plus_client_chain_clears_028_kill_bar(tmp_path: Path):
    dest = tmp_path / "demo"
    shutil.copytree(
        DEMO,
        dest,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", ".conduit"),
    )
    packet = json.loads(SAMPLE_PACKET.read_text(encoding="utf-8"))
    files = prune_by_imports(dest, ["openai"])
    report = apply_packet(dest, packet, dry_run=False, file_allowlist=files or None)
    assert report.files_modified

    src = dest / "src" / "ai_client.py"
    after_apply = src.read_text(encoding="utf-8")
    assert "ChatCompletion.create" not in after_apply
    assert "openai.chat.completions.create" in after_apply
    assert "gpt-4o" in after_apply
    assert "max_completion_tokens" in after_apply
    assert "openai==1.0.0" in (dest / "requirements.txt").read_text(encoding="utf-8")

    chain = apply_openai_client_chain(
        dest, [src], to_version=str(packet.get("to_version") or "")
    )
    assert chain.changes
    final = src.read_text(encoding="utf-8")
    assert "from openai import OpenAI" in final
    assert "client = OpenAI(" in final
    assert "client.chat.completions.create" in final
    assert "openai.ChatCompletion.create" not in final
    assert "ChatCompletion.create" not in final
    assert 'response["choices"]' not in final
    assert "response.choices[0].message.content" in final


def test_ast_param_rename_kept_when_function_target_matches_usage():
    """Scope must not drop AST_PARAM_RENAME solely because old_param is unused."""
    rules = [
        {
            "type": "AST_PARAM_RENAME",
            "target_files": ["*.py"],
            "function_target": "chat.completions.create",
            "old_param": "max_tokens",
            "new_param": "max_completion_tokens",
        }
    ]
    source = {
        "model_ids": ["gpt-4-0613"],
        "api_patterns": ["openai.ChatCompletion.create", "ChatCompletion.create"],
    }
    result = filter_rules_to_source(rules, source)
    assert result.kept == 1
    assert result.rules[0]["type"] == "AST_PARAM_RENAME"
