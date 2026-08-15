"""Normalize grounding rules + evidence helpers (no invented fallbacks)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conduit.detect.modules.openai.models_legacy import ChangeType, RawSignal, Severity
from conduit.detect.modules.openai.normalize import default_rules_for
from conduit.detect.modules.openai.workers.base import fixtures_dir
from conduit.detect.modules.openai.workers.openapi_diff import (
    OpenAPIDiffWorker,
    _diff_paths,
    _load_openapi,
)
from conduit.packet.evidence import host_allowed, html_to_text
from conduit.packet.synthesize import merge_packet_rules, synthesize_from_evidence
from conduit.patcher.string_replace import exact_replace


def test_api_breaking_path_emits_string_replace_when_both_sides():
    signal = RawSignal(
        vendor="openai",
        change_type=ChangeType.API_BREAKING,
        severity=Severity.CRITICAL,
        affected_pattern="/v1/engines",
        replacement_pattern="/v1/models",
    )
    rules = default_rules_for(signal)
    assert len(rules) == 1
    assert rules[0]["type"] == "EXACT_STRING_REPLACE"
    assert rules[0]["match"] == "/v1/engines"
    assert rules[0]["replace"] == "/v1/models"


def test_api_breaking_path_no_rules_without_replacement():
    signal = RawSignal(
        vendor="openai",
        change_type=ChangeType.API_BREAKING,
        severity=Severity.CRITICAL,
        affected_pattern="/v1/edits",
        replacement_pattern=None,
    )
    assert default_rules_for(signal) == []


def test_param_rename_no_rules_without_function_targets():
    signal = RawSignal(
        vendor="openai",
        change_type=ChangeType.PARAM_RENAME,
        severity=Severity.CRITICAL,
        affected_pattern="max_tokens",
        replacement_pattern="max_completion_tokens",
        extra={
            "old_param": "max_tokens",
            "new_param": "max_completion_tokens",
        },
    )
    assert default_rules_for(signal) == []


def test_param_rename_uses_explicit_target_only():
    signal = RawSignal(
        vendor="openai",
        change_type=ChangeType.PARAM_RENAME,
        severity=Severity.CRITICAL,
        affected_pattern="max_tokens",
        replacement_pattern="max_completion_tokens",
        extra={
            "old_param": "max_tokens",
            "new_param": "max_completion_tokens",
            "function_target": "openai.chat.completions.create",
        },
    )
    rules = default_rules_for(signal)
    assert len(rules) == 1
    assert rules[0]["function_target"] == "openai.chat.completions.create"


def test_openapi_fixture_diff_emits_engines_and_max_tokens():
    prev = _load_openapi(fixtures_dir() / "openapi" / "previous.yaml")
    latest = _load_openapi(fixtures_dir() / "openapi" / "latest.yaml")
    signals = _diff_paths(prev, latest)
    types = {s.change_type for s in signals}
    assert ChangeType.API_BREAKING in types
    assert ChangeType.PARAM_RENAME in types
    engines = [s for s in signals if s.affected_pattern == "/v1/engines"]
    assert engines
    # Removal without replacement → no apply rules from normalize
    assert default_rules_for(engines[0]) == []
    rename = [s for s in signals if s.affected_pattern == "max_tokens"]
    assert rename
    # Shared-path renames now carry grounded path→SDK function_targets
    rules = default_rules_for(rename[0])
    assert any(r.get("type") == "AST_PARAM_RENAME" for r in rules)
    assert any(r.get("function_target") for r in rules)


def test_openapi_demo_worker_uses_fixtures():
    signals = OpenAPIDiffWorker().run(demo=True)
    assert any(s.affected_pattern == "/v1/engines" for s in signals)
    assert any(s.affected_pattern == "max_tokens" for s in signals)


def test_endpoint_exact_replace_in_source():
    src = 'return "/v1/engines"\n'
    out, n = exact_replace(src, "/v1/engines", "/v1/models")
    assert n == 1
    assert out == 'return "/v1/models"\n'


def test_host_allowed_github_when_listed():
    hosts = ["platform.openai.com", "github.com"]
    assert host_allowed("https://platform.openai.com/docs/deprecations", hosts)
    assert host_allowed("https://github.com/openai/openai-python", hosts)
    assert host_allowed("https://github.com/other/repo", hosts)
    assert not host_allowed("https://github.com/", hosts)
    assert not host_allowed("https://evil.example/openai", hosts)


def test_html_to_text_strips_scripts():
    title, text = html_to_text(
        "<html><head><title>T</title><script>x</script></head>"
        "<body><p>Hello</p></body></html>"
    )
    assert title == "T"
    assert "Hello" in text
    assert "x" not in text


def test_merge_packet_rules_scrape_match_wins():
    base = [
        {
            "type": "EXACT_STRING_REPLACE",
            "match": "gpt-4-0613",
            "replace": "gpt-4o",
            "target_files": ["*.py"],
        }
    ]
    llm = [
        {
            "type": "EXACT_STRING_REPLACE",
            "match": "gpt-4-0613",
            "replace": "gpt-5.6-sol",
            "target_files": ["*.py"],
        },
        {
            "type": "AST_CALL_REWRITE",
            "old_callee": "openai.ChatCompletion.create",
            "new_callee": "client.chat.completions.create",
            "target_files": ["*.py"],
        },
    ]
    merged = merge_packet_rules(base, llm)
    exact = [r for r in merged if r["type"] == "EXACT_STRING_REPLACE"]
    assert len(exact) == 1
    assert exact[0]["replace"] == "gpt-4o"
    assert any(r["type"] == "AST_CALL_REWRITE" for r in merged)


def test_synthesize_from_evidence_merges_mocked_llm(monkeypatch):
    base = {
        "packet_id": "openai-0.28.1-1.0.0",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "sources": [],
        "notes": "from signals",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "old-model",
                "replace": "new-model",
                "target_files": ["*.py"],
            }
        ],
    }

    class FakeClient:
        def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
            return {
                "notes": "from evidence",
                "sources": [{"url": "https://platform.openai.com/docs/deprecations", "kind": "docs"}],
                "rules": [
                    {
                        "type": "AST_CALL_REWRITE",
                        "target_files": ["*.py"],
                        "old_callee": "openai.ChatCompletion.create",
                        "new_callee": "client.chat.completions.create",
                    }
                ],
            }

    monkeypatch.setattr(
        "conduit.llm.get_llm_client", lambda: FakeClient()
    )

    from conduit.packet import evidence as ev

    monkeypatch.setattr(
        ev,
        "build_evidence",
        lambda **kwargs: (
            [
                ev.EvidenceDoc(
                    url="https://platform.openai.com/docs/deprecations",
                    title="Deprecations",
                    text="ChatCompletion.create -> client.chat.completions.create",
                    kind="seed",
                )
            ],
            [],
        ),
    )

    packet, warnings = synthesize_from_evidence(
        package="openai",
        from_version="0.28.1",
        to_version="1.0.0",
        ecosystem="pypi",
        signals=[],
        base=base,
    )
    assert any("enrichment" in w for w in warnings)
    assert any(r["type"] == "AST_CALL_REWRITE" for r in packet["rules"])
    assert any(r.get("match") == "old-model" for r in packet["rules"])
