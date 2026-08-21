"""Usage dossier seeding for scoped LLM client enrichment."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.detect.client_state import (
    PackageClientState,
    build_usage_dossier,
    _agent_enrich,
)
from conduit.llm.executors import RepoToolExecutor
from conduit.llm.tools import agent_tools, conduit_function_tools


def test_build_usage_dossier_includes_hits(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "import openai\n"
        "openai.ChatCompletion.create(model='gpt-4')\n"
        "path = '/v1/engines'\n",
        encoding="utf-8",
    )
    state = PackageClientState(
        package="openai",
        model_ids=["gpt-4"],
        api_patterns=["ChatCompletion"],
        import_files=["app.py"],
    )
    dossier = build_usage_dossier(
        tmp_path, "openai", state, [tmp_path / "app.py"]
    )
    assert "app.py" in dossier["path_allowlist"]
    assert dossier["already_found"]["model_ids"] == ["gpt-4"]
    kinds = {h["kind"] for h in dossier["hits"]}
    assert "import" in kinds or "api" in kinds or "model" in kinds
    tokens = {h["token"] for h in dossier["hits"]}
    assert any("ChatCompletion" in t or "/v1/" in t or "gpt-4" in t for t in tokens)


def test_dossier_allowlist_prefers_imports(tmp_path: Path):
    (tmp_path / "imp.py").write_text("import openai\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "cfg.json").write_text('{"a": 1}\n', encoding="utf-8")
    state = PackageClientState(
        package="openai",
        import_files=["imp.py"],
    )
    dossier = build_usage_dossier(
        tmp_path,
        "openai",
        state,
        [tmp_path / "imp.py", tmp_path / "other.py", tmp_path / "cfg.json"],
    )
    assert dossier["path_allowlist"][0] == "imp.py"


def test_enrich_scoped_tools_have_no_list_files():
    names = {t.get("name") for t in conduit_function_tools(mode="enrich_scoped")}
    assert names == {"read_file", "grep"}
    agent = agent_tools(mode="enrich_scoped")
    assert all(t.get("type") == "function" for t in agent)
    assert not any(t.get("type") == "web_search" for t in agent)


def test_executor_rejects_outside_dossier_allowlist(tmp_path: Path):
    (tmp_path / "in.py").write_text("ok\n", encoding="utf-8")
    (tmp_path / "out.py").write_text("nope\n", encoding="utf-8")
    ex = RepoToolExecutor(
        root=tmp_path,
        allow_writes=False,
        path_allowlist={"in.py"},
    )
    ok = json.loads(ex("read_file", {"path": "in.py"}))
    assert ok.get("contents") == "ok\n"
    bad = json.loads(ex("read_file", {"path": "out.py"}))
    assert "error" in bad
    assert "allowlist" in bad["error"].lower()


def test_agent_enrich_merges_corpus_gated(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text(
        "import openai\n"
        "client.chat.completions.create(model='gpt-4o')\n",
        encoding="utf-8",
    )
    state = PackageClientState(
        package="openai",
        model_ids=[],
        api_patterns=[],
        import_files=["app.py"],
        source="regex",
    )

    class _FakeClient:
        def run_agent(self, **_kwargs):
            return {
                "model_ids": ["gpt-4o", "not-in-repo-xyz"],
                "api_patterns": ["chat.completions", "invented.api"],
                "usages": [],
            }

    monkeypatch.setattr(
        "conduit.llm.client.get_llm_client", lambda: _FakeClient()
    )
    monkeypatch.setattr(
        "conduit.llm.client.attach_llm_log", lambda c, _log: c
    )
    # Avoid known-id filtering rejecting gpt-4o in some envs
    monkeypatch.setattr(
        "conduit.detect.vendor_profile.collect_known_ids",
        lambda *_a, **_k: set(),
    )

    out = _agent_enrich(
        state, root=tmp_path, files=[tmp_path / "app.py"], log=None
    )
    assert "gpt-4o" in out.model_ids
    assert "not-in-repo-xyz" not in out.model_ids
    assert "chat.completions" in out.api_patterns
    assert "invented.api" not in out.api_patterns
    assert out.source == "agent"


def test_agent_enrich_skips_when_dossier_complete(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text(
        "import openai\n"
        "openai.ChatCompletion.create(model='gpt-4')\n",
        encoding="utf-8",
    )
    state = PackageClientState(
        package="openai",
        model_ids=["gpt-4"],
        api_patterns=["ChatCompletion.create"],
        import_files=["app.py"],
        source="regex",
    )
    called = {"n": 0}

    class _FakeClient:
        def run_agent(self, **_kwargs):
            called["n"] += 1
            return {"model_ids": [], "api_patterns": [], "usages": []}

    monkeypatch.setattr(
        "conduit.llm.client.get_llm_client", lambda: _FakeClient()
    )
    monkeypatch.setattr(
        "conduit.llm.client.attach_llm_log", lambda c, _log: c
    )
    monkeypatch.setattr(
        "conduit.detect.client_state._dossier_enrich_complete",
        lambda _d: True,
    )
    logs: list[str] = []
    out = _agent_enrich(
        state, root=tmp_path, files=[tmp_path / "app.py"], log=logs.append
    )
    assert called["n"] == 0
    assert any("skipped" in line.lower() for line in logs)
    assert any("dossier complete" in n for n in out.notes)
    assert out.model_ids == ["gpt-4"]


def test_agent_enrich_runs_when_gaps_exist(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text(
        "import openai\n",
        encoding="utf-8",
    )
    state = PackageClientState(
        package="openai",
        model_ids=[],
        api_patterns=[],
        import_files=["app.py"],
        source="regex",
    )
    called = {"n": 0}

    class _FakeClient:
        def run_agent(self, **_kwargs):
            called["n"] += 1
            return {"model_ids": [], "api_patterns": [], "usages": []}

    monkeypatch.setattr(
        "conduit.llm.client.get_llm_client", lambda: _FakeClient()
    )
    monkeypatch.setattr(
        "conduit.llm.client.attach_llm_log", lambda c, _log: c
    )
    monkeypatch.setattr(
        "conduit.detect.vendor_profile.collect_known_ids",
        lambda *_a, **_k: set(),
    )
    monkeypatch.setattr(
        "conduit.detect.client_state._dossier_enrich_complete",
        lambda _d: False,
    )
    _agent_enrich(
        state,
        root=tmp_path,
        files=[tmp_path / "app.py"],
        log=None,
    )
    assert called["n"] == 1

