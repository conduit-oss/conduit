"""Model-agnostic LLM client for packet synth, self-correct, and test gen."""

from __future__ import annotations

from conduit.llm.client import LlmClient, attach_llm_log, get_llm_client, resolve_provider
from conduit.llm.executors import RepoToolExecutor
from conduit.llm.json_util import extract_json_object, extract_json_payload
from conduit.llm.tools import agent_tools, resolve_max_turns, resolve_reasoning_effort

__all__ = [
    "LlmClient",
    "RepoToolExecutor",
    "agent_tools",
    "get_llm_client",
    "attach_llm_log",
    "resolve_provider",
    "resolve_max_turns",
    "resolve_reasoning_effort",
    "extract_json_object",
    "extract_json_payload",
]
