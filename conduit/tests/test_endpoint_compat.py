"""Model endpoint docs parsing, compat adjustments, PR rationale, repair research."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.endpoint_compat import apply_endpoint_compat
from conduit.detect.modules.openai.model_docs import (
    map_api_patterns_to_routes,
    model_supports_routes,
    parse_endpoints_markdown,
    parse_models_catalog,
)
from conduit.detect.modules.openai.models_legacy import ChangeType, RawSignal, Severity
from conduit.detect.modules.openai.normalize import default_rules_for
from conduit.packet.evidence import EvidenceDoc
from conduit.packet.synthesize import packet_from_signals
from conduit.patcher.engine import PatchReport
from conduit.pr_generator import build_pr_body


TERRA_LIKE_MD = """
# GPT-5.6 Terra

## Endpoints

| Endpoint | Route | Support |
| --- | --- | --- |
| Chat Completions | `v1/chat/completions` | Supported |
| Responses | `v1/responses` | Supported |
| Embeddings | `v1/embeddings` | Not supported |
| Completions (legacy) | `v1/completions` | Not supported |
"""


def test_parse_endpoints_markdown():
    endpoints = parse_endpoints_markdown(TERRA_LIKE_MD)
    assert endpoints["v1/chat/completions"] is True
    assert endpoints["v1/embeddings"] is False
    assert model_supports_routes(endpoints, ["v1/chat/completions"])
    assert not model_supports_routes(endpoints, ["v1/embeddings"])


def test_map_api_patterns_to_routes():
    routes = map_api_patterns_to_routes(
        ["chat.completions", "ChatCompletion", "/v1/embeddings", "embeddings.create"]
    )
    assert routes == ["v1/chat/completions", "v1/embeddings"]


def test_parse_models_catalog_links():
    catalog = parse_models_catalog(
        "- [GPT-4o](/api/docs/models/gpt-4o.md)\n"
        "- [Terra](/api/docs/models/gpt-5.6-terra.md)\n"
    )
    assert "gpt-4o" in catalog
    assert "gpt-5.6-terra" in catalog


def test_default_rules_include_reason():
    signal = RawSignal(
        vendor="openai",
        change_type=ChangeType.MODEL_DEPRECATION,
        severity=Severity.CRITICAL,
        affected_pattern="gpt-4-0613",
        replacement_pattern="gpt-4o",
        description="Replace gpt-4-0613 with gpt-4o from deprecations.",
        source_url="https://platform.openai.com/docs/deprecations",
    )
    rules = default_rules_for(signal)
    assert rules[0]["reason"]
    assert "gpt-4o" in rules[0]["reason"]


def test_compat_keeps_compatible_replacement_with_reason():
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            affected_pattern="gpt-4-0613",
            replacement_pattern="gpt-4o",
            description="Model gpt-4-0613 deprecated; replace with gpt-4o",
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "target_files": ["*.py"],
                    "match": "gpt-4-0613",
                    "replace": "gpt-4o",
                }
            ],
        )
    ]
    state = PackageClientState(
        package="openai",
        model_ids=["gpt-4-0613"],
        api_patterns=["chat.completions"],
    )
    out, notes = apply_endpoint_compat(signals, client_state=state, demo=True)
    assert len(out) == 1
    assert out[0].replacement_pattern == "gpt-4o"
    assert out[0].suggested_rules[0]["reason"]
    assert any("gpt-4o" in n for n in notes)


def test_compat_swaps_incompatible_replacement():
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            affected_pattern="gpt-4-0613",
            replacement_pattern="gpt-incompat-chat",
            description="bad suggestion",
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "target_files": ["*.py"],
                    "match": "gpt-4-0613",
                    "replace": "gpt-incompat-chat",
                }
            ],
        )
    ]
    state = PackageClientState(
        package="openai",
        model_ids=["gpt-4-0613"],
        api_patterns=["chat.completions"],
    )
    out, notes = apply_endpoint_compat(signals, client_state=state, demo=True)
    assert out[0].replacement_pattern == "gpt-4o"
    assert out[0].suggested_rules[0]["replace"] == "gpt-4o"
    assert "gpt-incompat-chat" in (out[0].description or "")
    assert any("gpt-incompat-chat" in n and "gpt-4o" in n for n in notes)


def test_packet_from_signals_carries_reason_and_notes():
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            affected_pattern="gpt-4-0613",
            replacement_pattern="gpt-4o",
            description="Chose gpt-4o for chat.completions support.",
            hints={"endpoint_compat_reason": "Chose gpt-4o for chat.completions support."},
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "target_files": ["*.py"],
                    "match": "gpt-4-0613",
                    "replace": "gpt-4o",
                    "reason": "Chose gpt-4o for chat.completions support.",
                }
            ],
        )
    ]
    packet = packet_from_signals(signals, package="openai")
    assert packet["rules"][0]["reason"]
    assert "Decision rationale" in (packet.get("notes") or "")


def test_build_pr_body_includes_rationale_and_sources():
    from conduit.test_runner import TestResult as RunnerResult

    packet: dict[str, Any] = {
        "packet_id": "demo",
        "package": "openai",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "notes": "Decision rationale:\n- kept gpt-4o",
        "sources": [
            {"url": "https://developers.openai.com/api/docs/models", "kind": "docs"},
            {"url": "https://platform.openai.com/docs/deprecations", "kind": "docs"},
        ],
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
                "reason": "Supports v1/chat/completions used by client.",
            }
        ],
    }
    body = build_pr_body(
        packet,
        PatchReport(),
        RunnerResult(
            runner="pytest",
            passed=True,
            returncode=0,
            stdout="",
            stderr="",
            command=["pytest"],
        ),
    )
    assert "### Rationale" in body
    assert "Supports v1/chat/completions" in body
    assert "### Sources" in body
    assert "developers.openai.com/api/docs/models" in body
    assert "### Notes" in body


def test_self_correct_researches_and_passes_evidence(tmp_path: Path, monkeypatch):
    from conduit.self_correct import verify_with_self_correct
    from conduit.test_runner import TestResult as RunnerResult

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert False\n", encoding="utf-8"
    )
    packet = {
        "packet_id": "p",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.1",
        "to_version": "1.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
                "reason": "deprecation",
            }
        ],
        "notes": "",
    }

    calls = {"n": 0}

    def fake_run_tests(root: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            return RunnerResult(
                runner="pytest",
                passed=False,
                returncode=1,
                stdout='model="gpt-4-0613" failed',
                stderr="",
                command=["pytest"],
            )
        return RunnerResult(
            runner="pytest",
            passed=True,
            returncode=0,
            stdout="",
            stderr="",
            command=["pytest"],
        )

    class FakeClient:
        def complete_json(self, system: str, user: str):
            payload = json.loads(user)
            assert "evidence" in payload
            assert "developers.openai.com" in payload["evidence"]
            return {
                "files": {
                    "tests/test_app.py": "def test_ok():\n    assert True\n",
                }
            }

    evidence_calls: list[dict[str, Any]] = []

    def fake_build_evidence(**kwargs):
        evidence_calls.append(kwargs)
        return (
            [
                EvidenceDoc(
                    url="https://developers.openai.com/api/docs/models/gpt-4o.md",
                    title="GPT-4o",
                    text="Endpoints chat completions Supported",
                    kind="seed",
                )
            ],
            [],
        )

    monkeypatch.setattr("conduit.self_correct.run_tests", fake_run_tests)
    monkeypatch.setattr("conduit.self_correct.get_llm_client", lambda: FakeClient())
    monkeypatch.setattr("conduit.packet.evidence.build_evidence", fake_build_evidence)

    result, corrected = verify_with_self_correct(
        tmp_path, packet, max_retries=2, verbose=True, log=lambda _m: None
    )
    assert result.passed
    assert corrected
    assert evidence_calls
    assert "Self-correct" in (packet.get("notes") or "")
