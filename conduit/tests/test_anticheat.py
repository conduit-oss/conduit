"""Separate anti-cheat: official SDK, fake clients, forbidden writes, f-string hide."""

from __future__ import annotations

from pathlib import Path

from conduit.anticheat.rules import reject_write
from conduit.anticheat.scan import run_anticheat, run_anticheat_mechanical
from conduit.self_correct import reject_self_correct_write
from conduit.text_tokens import obfuscated_forbidden_tokens, reconstructed_literals


def _packet() -> dict:
    return {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "/v1/engines",
                "replace": "/v1/models",
            },
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
            },
        ],
    }


def test_reject_fake_response_client():
    previous = "import openai\n\ndef chat():\n    return openai.ChatCompletion.create()\n"
    content = (
        "import requests\n"
        "class _FakeResponse:\n"
        "    pass\n"
        "def _fallback_response(method, path, payload):\n"
        "    return {'id': 'chatcmpl-fallback'}\n"
    )
    reason = reject_write(
        "packages/openai_text/client.py",
        content,
        packet=_packet(),
        previous=previous,
    )
    assert reason is not None
    assert "dropped official" in reason or "fake" in reason.lower()


def test_allow_official_sdk_migration():
    previous = "import openai\nresp = openai.ChatCompletion.create(model='gpt-4')\n"
    content = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "resp = client.chat.completions.create(model='gpt-4o', messages=[])\n"
    )
    assert (
        reject_write(
            "chat.py", content, packet=_packet(), previous=previous
        )
        is None
    )


def test_fstring_obfuscates_engines():
    text = "route = f\"/{'engines'}\"\n"
    assert "/engines" in reconstructed_literals(text) or any(
        "engines" in x for x in reconstructed_literals(text)
    )
    hidden = obfuscated_forbidden_tokens(text, ["/engines", "/v1/engines"])
    assert hidden


def test_reject_packet_json_write():
    reason = reject_self_correct_write(
        "packets/openai-pypi-3.3.1.json",
        '{"packet_id": "x"}',
        packet=_packet(),
    )
    assert reason is not None
    assert "packet" in reason.lower() or "cannot edit" in reason


def test_reject_jsonl_seed_write():
    reason = reject_write(
        "services/knowledge_rag/data/faq_seed.jsonl",
        '{"text": "davinci"}\n',
        packet=_packet(),
        previous="",
    )
    assert reason is not None


def test_mechanical_scan_flags_fake_client(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")
    (tmp_path / "client.py").write_text(
        "import requests\n"
        "def _fallback_response(method, path, payload):\n"
        "    return {'id': 'chatcmpl-fallback', 'choices': []}\n"
        "def request_json(method, path):\n"
        "    if path.endswith('/chat/completions'):\n"
        "        return _fallback_response(method, path, {})\n",
        encoding="utf-8",
    )
    report = run_anticheat_mechanical(tmp_path, _packet())
    assert report.failed
    assert any("fake" in f.lower() or "official openai" in f.lower() for f in report.findings)


def test_llm_auditor_adds_findings(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text("from openai import OpenAI\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")

    def _fake_audit(*_a, **_k):
        return ["app.py: cheat — dummy HTTP stub"]

    monkeypatch.setattr(
        "conduit.anticheat.llm_audit.llm_audit_findings", _fake_audit
    )
    report = run_anticheat(tmp_path, _packet(), llm=True)
    assert report.failed
    assert any("dummy HTTP stub" in f for f in report.findings)


def test_mechanical_finding_not_cleared_by_empty_auditor(tmp_path: Path, monkeypatch):
    (tmp_path / "client.py").write_text(
        "def _fallback_response():\n    return {'id': 'chatcmpl-fallback'}\n",
        encoding="utf-8",
    )

    def _clean(*_a, **_k):
        return []

    monkeypatch.setattr(
        "conduit.anticheat.llm_audit.llm_audit_findings", _clean
    )
    report = run_anticheat(tmp_path, _packet(), llm=True)
    assert report.failed
    assert report.source in {"mechanical", "mixed"}
