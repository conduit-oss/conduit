"""Repair research gate and prompt evidence tests."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.llm.executors import RepoToolExecutor
from conduit.self_correct import _llm_suggest_fixes
from conduit.test_runner import TestResult


def test_repair_rejects_write_before_fetch(tmp_path: Path):
    executor = RepoToolExecutor(
        root=tmp_path,
        allow_writes=True,
        require_research_before_write=True,
        preloaded_evidence_chars=500,
    )
    out = json.loads(
        executor(
            "write_file",
            {"path": "app.py", "contents": "print('hi')\n"},
        )
    )
    assert "error" in out
    assert "research" in out["error"].lower()


def test_repair_allows_write_after_fetch(tmp_path: Path, monkeypatch):
    executor = RepoToolExecutor(
        root=tmp_path,
        allow_writes=True,
        require_research_before_write=True,
        preloaded_evidence_chars=500,
    )

    def _fake_fetch(url: str) -> str:
        return f"docs for {url}"

    monkeypatch.setattr(
        "conduit.context.fetch.fetch_url",
        _fake_fetch,
    )
    json.loads(executor("fetch_url", {"url": "https://developers.openai.com/api/docs/deprecations"}))
    out = json.loads(
        executor(
            "write_file",
            {"path": "app.py", "contents": "print('ok')\n"},
        )
    )
    assert out.get("ok") is True


def test_repair_prompt_includes_migration_docs(monkeypatch, tmp_path: Path):
    captured: dict = {}

    class FakeClient:
        def run_agent(self, **kwargs):
            captured["user"] = json.loads(kwargs["user"])
            return {"files": {}}

    monkeypatch.setattr("conduit.self_correct.get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(
        "conduit.self_correct.attach_llm_log", lambda client, _log=None: client
    )

    result = TestResult(
        runner="pytest",
        passed=False,
        returncode=1,
        stdout="FAILED test_foo - Completion.create unsupported",
        stderr="",
        command=["pytest"],
    )
    packet = {
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [],
    }

    _llm_suggest_fixes(
        root=tmp_path,
        test_result=result,
        packet=packet,
        files={"app.py": "openai.Completion.create()"},
        seed_urls=["https://example.com/deprecations"],
        migration_evidence={
            "migration_docs": "### SEED https://example.com\nbody",
            "code_examples": "client.chat.completions.create()",
            "openapi_structs": "",
            "model_endpoints": "",
        },
        preloaded_evidence_chars=100,
    )
    assert "migration_docs" in captured["user"]
    assert "code_examples" in captured["user"]
    assert "fetch_url" in captured["user"]["instructions"].lower()
