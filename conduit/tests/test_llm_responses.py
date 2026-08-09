"""OpenAI Responses client + tool registry (no live network)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conduit.llm.client import _OpenAIResponsesClient, _function_calls, _output_text
from conduit.llm.executors import RepoToolExecutor
from conduit.llm.tools import agent_tools, openai_builtin_tools, resolve_reasoning_effort
from conduit.repair_ignore import IgnoreList


def test_default_reasoning_effort_high(monkeypatch):
    monkeypatch.delenv("CONDUIT_LLM_REASONING_EFFORT", raising=False)
    assert resolve_reasoning_effort() == "high"


def test_reasoning_effort_override(monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_REASONING_EFFORT", "xhigh")
    assert resolve_reasoning_effort() == "xhigh"


def test_builtin_tools_default_no_remote(monkeypatch):
    monkeypatch.delenv("CONDUIT_LLM_REMOTE_TOOLS", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_VECTOR_STORE_IDS", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_MCP_URL", raising=False)
    types = {t["type"] for t in openai_builtin_tools()}
    assert "web_search" in types
    assert "code_interpreter" in types
    assert "apply_patch" in types
    assert "computer_use" not in types
    assert "hosted_shell" not in types
    assert "file_search" not in types
    assert "mcp" not in types
    assert "image_generation" not in types


def test_builtin_tools_env_gated(monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_REMOTE_TOOLS", "1")
    monkeypatch.setenv("CONDUIT_LLM_VECTOR_STORE_IDS", "vs_a, vs_b")
    monkeypatch.setenv("CONDUIT_LLM_MCP_URL", "https://mcp.example/v1")
    tools = openai_builtin_tools()
    by_type = {t["type"]: t for t in tools}
    assert "computer_use" in by_type
    assert "hosted_shell" in by_type
    assert by_type["file_search"]["vector_store_ids"] == ["vs_a", "vs_b"]
    assert by_type["mcp"]["server_url"] == "https://mcp.example/v1"


def test_agent_tools_modes():
    enrich_names = {
        t["name"] for t in agent_tools(mode="enrich") if t.get("type") == "function"
    }
    repair_names = {
        t["name"]
        for t in agent_tools(mode="self_correct")
        if t.get("type") == "function"
    }
    assert "read_file" in enrich_names
    assert "fetch_url" in enrich_names
    assert "write_file" not in enrich_names
    assert "run_tests" not in enrich_names
    assert "write_file" in repair_names
    assert "run_tests" in repair_names


def test_repo_executor_read_write(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ex = RepoToolExecutor(root=tmp_path, allow_writes=True, allow_run_tests=False)
    listed = json.loads(ex("list_files", {"directory": ".", "glob": "*.py"}))
    assert "app.py" in listed["files"]
    read = json.loads(ex("read_file", {"path": "app.py"}))
    assert "x = 1" in read["contents"]
    written = json.loads(
        ex("write_file", {"path": "app.py", "contents": "x = 2\n"})
    )
    assert written["ok"] is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    denied = json.loads(
        RepoToolExecutor(root=tmp_path, allow_writes=False)(
            "write_file", {"path": "app.py", "contents": "nope"}
        )
    )
    assert "error" in denied


def test_repo_executor_respects_ignore(tmp_path: Path):
    (tmp_path / "oracle.py").write_text("LEGACY = 'a'\n", encoding="utf-8")
    ignore = IgnoreList(paths={"oracle.py"})
    ex = RepoToolExecutor(root=tmp_path, ignore=ignore, allow_writes=True)
    out = json.loads(ex("read_file", {"path": "oracle.py"}))
    assert "ignored" in out["error"]


def test_function_calls_and_output_text_helpers():
    resp = SimpleNamespace(
        output_text="",
        output=[
            SimpleNamespace(
                type="function_call",
                name="read_file",
                call_id="call_1",
                arguments='{"path": "a.py"}',
            ),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text='{"ok": true}')],
            ),
        ],
    )
    calls = _function_calls(resp)
    assert calls == [
        {"name": "read_file", "call_id": "call_1", "arguments": {"path": "a.py"}}
    ]
    assert '{"ok": true}' in _output_text(resp)


def test_responses_agent_tool_loop(monkeypatch, tmp_path: Path):
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")

    class FakeResponsesAPI:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert kwargs["model"] == "gpt-5.4-mini"
                assert kwargs["reasoning"] == {"effort": "high"}
                assert any(
                    isinstance(t, dict) and t.get("type") == "web_search"
                    for t in kwargs.get("tools") or []
                )
                return SimpleNamespace(
                    id="resp_1",
                    output_text="",
                    output=[
                        SimpleNamespace(
                            type="function_call",
                            name="read_file",
                            call_id="c1",
                            arguments=json.dumps({"path": "a.py"}),
                        )
                    ],
                )
            # second turn: previous_response_id + function output
            assert kwargs.get("previous_response_id") == "resp_1"
            assert kwargs["input"][0]["type"] == "function_call_output"
            assert "print(1)" in kwargs["input"][0]["output"]
            return SimpleNamespace(
                id="resp_2",
                output_text='{"files": {"a.py": "print(2)\\n"}, "packet_patch": {}}',
                output=[],
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponsesAPI()

    monkeypatch.setitem(
        __import__("sys").modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI),
    )

    client = _OpenAIResponsesClient(
        model="gpt-5.4-mini",
        api_key="sk-test",
        reasoning_effort="high",
    )
    # Force re-import path for OpenAI inside _sdk
    client._client = FakeOpenAI()

    ex = RepoToolExecutor(root=tmp_path, allow_writes=True)
    data = client.run_agent(
        system="repair",
        user="fix it",
        tools=agent_tools(mode="self_correct"),
        tool_executor=ex,
        max_turns=4,
    )
    assert data["files"]["a.py"] == "print(2)\n"


def test_default_openai_model(monkeypatch):
    monkeypatch.delenv("CONDUIT_LLM_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "openai")
    # Avoid needing a real openai package shape beyond import
    import conduit.llm.client as client_mod

    monkeypatch.setattr(client_mod, "resolve_provider", lambda: "openai")
    # If openai import fails in env, skip
    try:
        import openai  # noqa: F401
    except ImportError:
        pytest.skip("openai package not installed")
    c = client_mod.get_llm_client()
    assert c is not None
    assert getattr(c, "model") == "gpt-5.4-mini"
    assert getattr(c, "reasoning_effort") == "high"
