"""Tests for llms.txt doc router and migration evidence builder."""

from __future__ import annotations

from conduit.packet.doc_augment import derive_callee_rules, parse_path_pairs_from_docs
from conduit.packet.doc_router import parse_llms_links, resolve_doc_urls, score_link
from conduit.packet.evidence import EvidenceDoc
from conduit.packet.migration_evidence import (
    extract_code_examples,
    extract_openapi_snippets,
)


def test_parse_llms_links():
    text = """
# Docs
- [Deprecations](https://developers.openai.com/api/docs/deprecations)
- [Chat guide](https://developers.openai.com/api/docs/guides/chat)
"""
    links = parse_llms_links(text)
    assert ("Deprecations", "https://developers.openai.com/api/docs/deprecations") in links


def test_doc_router_scores_deprecations_for_completion_usage(monkeypatch):
    index = """
- [Deprecations](https://developers.openai.com/api/docs/deprecations)
- [Models](https://developers.openai.com/api/docs/models)
- [Responses](https://developers.openai.com/api/docs/guides/responses)
"""
    monkeypatch.setattr(
        "conduit.packet.doc_router._fetch_index",
        lambda _url: index,
    )
    urls = resolve_doc_urls(
        context_chunks=["Completion.create /v1/completions deprecated chat"],
        profile_seeds=["https://developers.openai.com/api/docs/deprecations"],
        max_urls=4,
    )
    assert any("deprecat" in u for u in urls)


def test_score_link_prefers_matching_tokens():
    score = score_link(
        "Chat completions migration",
        "https://developers.openai.com/api/docs/guides/chat-completions",
        context_tokens={"completion", "chat", "deprecated"},
    )
    assert score >= 2.0


def test_extract_code_examples_from_docs():
    docs = [
        EvidenceDoc(
            url="https://example.com/guide",
            title="Guide",
            text="Use this:\n```python\nclient.chat.completions.create(model='gpt-4o')\n```\n",
            kind="seed",
        )
    ]
    examples = extract_code_examples(docs)
    assert len(examples) == 1
    assert "chat.completions.create" in examples[0].text


def test_parse_path_pairs_from_docs():
    text = "Migrate /v1/completions → /v1/chat/completions for chat models."
    pairs = parse_path_pairs_from_docs(text, source_url="https://example.com/deprecations")
    assert pairs
    assert pairs[0][0] == "/v1/completions"
    assert pairs[0][1] == "/v1/chat/completions"


def test_derive_callee_rules_from_path_pair():
    rules = derive_callee_rules(
        path_pairs=[
            (
                "/v1/completions",
                "/v1/chat/completions",
                "Endpoint /v1/completions → /v1/chat/completions. Source: https://example.com",
            )
        ],
        api_patterns=["Completion.create"],
    )
    assert rules
    assert any(
        r.get("type") == "AST_CALL_REWRITE"
        and r.get("old_callee") == "Completion.create"
        for r in rules
    )


def test_derive_callee_rules_rejects_path_as_new_callee():
    rules = derive_callee_rules(
        path_pairs=[
            (
                "/v1/fine-tunes",
                "/v1/fine_tuning/jobs",
                "Endpoint migration",
            )
        ],
        api_patterns=["/v1/fine-tunes", "/v1/fine_tuning/jobs", "openai.FineTune.list"],
    )
    for rule in rules:
        assert not str(rule.get("new_callee") or "").startswith("/")
        assert not str(rule.get("old_callee") or "").startswith("/")


def test_openapi_snippets_from_fixture():
    snippets = extract_openapi_snippets(
        paths=["/v1/chat/completions"],
        demo=True,
    )
    assert snippets
    assert "/v1/chat/completions" in snippets[0].text
