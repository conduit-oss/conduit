"""OpenAI Responses built-in tools + Conduit function-tool schemas."""

from __future__ import annotations

import os
from typing import Any, Literal

ToolMode = Literal["self_correct", "enrich", "readonly"]

_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})


def resolve_reasoning_effort(default: str = "high") -> str:
    raw = os.environ.get("CONDUIT_LLM_REASONING_EFFORT", "").strip().lower()
    if raw in _REASONING_EFFORTS:
        return raw
    return default if default in _REASONING_EFFORTS else "high"


def openai_builtin_tools() -> list[dict[str, Any]]:
    """Built-in Responses tools. Hosted/remote tools are env-gated."""
    tools: list[dict[str, Any]] = [
        {"type": "web_search"},
        {"type": "code_interpreter", "container": {"type": "auto"}},
        {"type": "apply_patch"},
    ]

    vs_raw = os.environ.get("CONDUIT_LLM_VECTOR_STORE_IDS", "").strip()
    if vs_raw:
        ids = [x.strip() for x in vs_raw.split(",") if x.strip()]
        if ids:
            tools.append({"type": "file_search", "vector_store_ids": ids})

    mcp = os.environ.get("CONDUIT_LLM_MCP_URL", "").strip()
    if mcp:
        tools.append({"type": "mcp", "server_url": mcp})

    remote = os.environ.get("CONDUIT_LLM_REMOTE_TOOLS", "").strip().lower()
    if remote in {"1", "true", "yes", "on"}:
        tools.append({"type": "computer_use"})
        tools.append({"type": "hosted_shell"})

    return tools


def _fn(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def conduit_function_tools(*, mode: ToolMode) -> list[dict[str, Any]]:
    """Local tools Conduit executes. Mode controls write/test access."""
    tools = [
        _fn(
            "list_files",
            "List files under a relative directory in the consumer repo "
            "(non-ignored paths only).",
            {
                "directory": {
                    "type": "string",
                    "description": "Relative directory (default '.').",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional glob, e.g. '**/*.py'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max paths to return (default 80).",
                },
            },
        ),
        _fn(
            "read_file",
            "Read a UTF-8 text file from the consumer repo by relative path.",
            {
                "path": {
                    "type": "string",
                    "description": "Relative file path.",
                },
            },
            required=["path"],
        ),
        _fn(
            "fetch_url",
            "HTTP GET a documentation or API URL and return truncated text.",
            {
                "url": {
                    "type": "string",
                    "description": "http(s) URL to fetch.",
                },
            },
            required=["url"],
        ),
    ]
    if mode == "self_correct":
        tools.extend(
            [
                _fn(
                    "write_file",
                    "Write full UTF-8 contents to a relative path in the consumer repo. "
                    "Ignored oracle/contract paths are rejected.",
                    {
                        "path": {"type": "string"},
                        "contents": {"type": "string"},
                    },
                    required=["path", "contents"],
                ),
                _fn(
                    "run_tests",
                    "Run the consumer repo's detected test suite "
                    "(python -m pytest -q when applicable).",
                    {},
                ),
            ]
        )
    return tools


def agent_tools(*, mode: ToolMode) -> list[dict[str, Any]]:
    """Built-ins + Conduit functions for a Responses agent turn."""
    return [*openai_builtin_tools(), *conduit_function_tools(mode=mode)]
