"""Tests for pre-apply impact analysis."""

from __future__ import annotations

from pathlib import Path

from conduit.patcher.impact.engine import analyze_impacts, merge_runtime_packet
from conduit.patcher.impact.mechanical import mechanical_impact_pass
from conduit.patcher.impact.templates import azure_bridge_module_body
from conduit.patcher.impact.vendor import default_banned_kwargs_on


def _openai_packet() -> dict:
    return {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "3.3.1",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "Completion.create",
                "new_callee": "chat.completions.create",
            },
        ],
    }


def test_vendor_default_banned_kwargs_includes_engine():
    packet = _openai_packet()
    bans = default_banned_kwargs_on(packet)
    assert "engine" in bans.get("chat.completions.create", [])


def test_mechanical_detects_engine_kwarg_on_rewritten_callee(tmp_path: Path):
    rel = "services/azure_bridge/client.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        "from openai import OpenAI\n\n"
        "def run():\n"
        "    client = OpenAI()\n"
        "    return client.chat.completions.create(engine='gpt-4', messages=[])\n",
        encoding="utf-8",
    )
    packet = _openai_packet()
    findings, rules, defer = mechanical_impact_pass(
        tmp_path,
        packet,
        planned_paths={rel},
        oracle_paths=set(),
        allowlist_paths=set(),
    )
    assert any("engine=" in str(f.get("detail", "")) for f in findings)
    assert not rules
    assert not defer


def test_mechanical_azure_classic_queues_fix_and_defer(tmp_path: Path):
    rel = "packages/azure_bridge/configure.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        'import openai\n\nopenai.api_type = "azure"\nopenai.api_base = "https://x.openai.azure.com"\n',
        encoding="utf-8",
    )
    packet = _openai_packet()
    findings, rules, defer = mechanical_impact_pass(
        tmp_path,
        packet,
        planned_paths=set(),
        oracle_paths={rel},
        allowlist_paths=set(),
    )
    assert defer == {rel}
    assert len(rules) == 1
    assert rules[0].get("type") == "FUNCTION_BODY_REPLACE"
    assert rules[0].get("target_files") == [rel]
    assert "AzureOpenAI" in str(rules[0].get("body", ""))
    assert any(f.get("kind") == "azure_classic_client" and f.get("action") == "fix" for f in findings)


def test_azure_template_satisfies_configure_contract():
    body = azure_bridge_module_body()
    assert 'api_type": "azure"' in body or '"api_type": "azure"' in body
    assert "api_version" in body
    assert "openai.api_type" not in body


def test_merge_runtime_packet_merges_anticheat():
    packet = {"anticheat": {"forbidden_tokens": ["a"]}}
    merged = merge_runtime_packet(
        packet,
        {"anticheat": {"banned_kwargs_on": {"chat.completions.create": ["engine"]}}},
    )
    assert merged["anticheat"]["forbidden_tokens"] == ["a"]
    assert merged["anticheat"]["banned_kwargs_on"]["chat.completions.create"] == ["engine"]


def test_analyze_impacts_use_llm_false(tmp_path: Path):
    rel = "packages/azure_bridge/module.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text('openai.api_type = "azure"\n', encoding="utf-8")
    packet = _openai_packet()
    impact = analyze_impacts(
        tmp_path,
        packet,
        file_allowlist=[path],
        log=lambda _m: None,
        use_llm=False,
    )
    assert rel in impact.defer_paths
    assert impact.post_rules
    assert not impact.blocked


def test_mechanical_skips_kwargs_covered_by_packet_rename(tmp_path: Path):
    rel = "src/ai_client.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        "from openai import OpenAI\n\n"
        "def complete(prompt):\n"
        "    client = OpenAI()\n"
        "    return client.chat.completions.create(\n"
        "        model='gpt-4o', messages=[], max_tokens=64\n"
        "    )\n",
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
                "type": "EXACT_STRING_REPLACE",
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            },
            {
                "type": "AST_PARAM_RENAME",
                "function_target": "chat.completions.create",
                "old_param": "max_tokens",
                "new_param": "max_completion_tokens",
            },
        ],
    }
    findings, rules, defer = mechanical_impact_pass(
        tmp_path,
        packet,
        planned_paths={rel},
        oracle_paths=set(),
        allowlist_paths=set(),
    )
    assert not any("max_tokens" in str(f.get("detail", "")) for f in findings)
    assert not rules
    assert not defer


def test_analyze_impacts_does_not_block_on_packet_covered_rename(
    tmp_path: Path, monkeypatch
):
    rel = "src/ai_client.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        'DEFAULT_MODEL = "gpt-4-0613"\n'
        "from openai import OpenAI\n\n"
        "def complete(prompt):\n"
        "    client = OpenAI()\n"
        "    return client.chat.completions.create(\n"
        "        model=DEFAULT_MODEL, messages=[], max_tokens=64\n"
        "    )\n",
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
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            },
            {
                "type": "AST_PARAM_RENAME",
                "target_files": ["*.py"],
                "function_target": "chat.completions.create",
                "old_param": "max_tokens",
                "new_param": "max_completion_tokens",
            },
        ],
    }

    def _fake_llm(**_kwargs):
        return (
            [
                {
                    "path": rel,
                    "kind": "legacy_kwargs_on_rewritten_callee",
                    "severity": "error",
                    "action": "fix",
                    "required": True,
                    "detail": (
                        f"{rel}:18 client.chat.completions.create still uses "
                        "max_tokens= (migrate to max_completion_tokens=)"
                    ),
                    "source": "llm",
                }
            ],
            [],
            [],
        )

    monkeypatch.setattr(
        "conduit.patcher.impact.engine.llm_impact_review", _fake_llm
    )
    impact = analyze_impacts(
        tmp_path,
        packet,
        file_allowlist=[path],
        log=lambda _m: None,
        use_llm=True,
    )
    assert not impact.blocked


def test_llm_defer_paths_ignored(tmp_path: Path, monkeypatch):
    rel = "podcast_ingest.py"
    path = tmp_path / rel
    path.write_text(
        "import openai\n"
        "openai.api_key = 'x'\n"
        "openai.Audio.transcribe(model='whisper-1', file=open('a'))\n",
        encoding="utf-8",
    )
    packet = _openai_packet()

    def _fake_llm(**_kwargs):
        return (
            [
                {
                    "path": rel,
                    "kind": "legacy_openai_module_config",
                    "action": "required",
                    "severity": "warning",
                    "required": True,
                    "detail": "api_key",
                    "source": "llm",
                }
            ],
            [],
            [rel],
        )

    monkeypatch.setattr(
        "conduit.patcher.impact.engine.llm_impact_review", _fake_llm
    )
    impact = analyze_impacts(
        tmp_path,
        packet,
        file_allowlist=[path],
        log=lambda _m: None,
        use_llm=True,
    )
    assert rel not in impact.defer_paths
    assert any(f.get("path") == rel for f in impact.findings)


def test_llm_impact_review_returns_empty_defer(monkeypatch):
    from conduit.patcher.impact import llm as llm_mod

    class _Client:
        def complete_json(self, **_k):
            return {
                "impacts": [],
                "post_rules": [],
                "defer_paths": ["podcast_ingest.py"],
            }

    monkeypatch.setattr(llm_mod, "get_llm_client", lambda: _Client())
    findings, rules, defer = llm_mod.llm_impact_review(
        packet=_openai_packet(),
        mechanical_findings=[],
        planned_paths=["podcast_ingest.py"],
        file_windows=[{"path": "podcast_ingest.py", "text": "import openai\n"}],
        log=lambda _m: None,
    )
    assert defer == []
    assert findings == []
    assert rules == []
