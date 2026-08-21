"""OpenAI Responses built-in tools + Conduit function-tool schemas."""

from __future__ import annotations

import os
from typing import Any, Literal

ToolMode = Literal[
    "self_correct", "enrich", "readonly", "anticheat_audit", "enrich_scoped"
]

_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})


def resolve_reasoning_effort(default: str = "high") -> str:
    raw = os.environ.get("CONDUIT_LLM_REASONING_EFFORT", "").strip().lower()
    if raw in _REASONING_EFFORTS:
        return raw
    return default if default in _REASONING_EFFORTS else "high"


def resolve_max_turns(default: int = 32) -> int:
    """Max Responses agent turns (env ``CONDUIT_LLM_MAX_TURNS``)."""
    raw = os.environ.get("CONDUIT_LLM_MAX_TURNS", "").strip()
    if not raw:
        return max(1, default)
    try:
        return max(1, min(int(raw), 128))
    except ValueError:
        return max(1, default)


def openai_builtin_tools() -> list[dict[str, Any]]:
    """Built-in Responses tools. Hosted/remote tools are env-gated.

    Note: OpenAI ``apply_patch`` is omitted — Conduit applies consumer-repo
    edits via local ``write_file`` / ``run_shell``, not the remote sandbox.
    Hosted ``computer_use`` / ``hosted_shell`` stay opt-in (not the lab on disk).
    """
    tools: list[dict[str, Any]] = [
        {"type": "web_search"},
        {"type": "code_interpreter", "container": {"type": "auto"}},
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
    if mode in {"anticheat_audit", "enrich_scoped"}:
        scope = (
            "migration audit log"
            if mode == "anticheat_audit"
            else "usage dossier allowlist"
        )
        return [
            _fn(
                "read_file",
                f"Read a UTF-8 text file that appears in the {scope} "
                "(other paths are rejected).",
                {
                    "path": {
                        "type": "string",
                        "description": "Relative file path from the allowlist.",
                    },
                },
                required=["path"],
            ),
            _fn(
                "grep",
                f"Search file contents under {scope} paths only "
                "(executor rejects other paths).",
                {
                    "pattern": {
                        "type": "string",
                        "description": "Literal or regex pattern to search for.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional file glob, e.g. '**/*.py'.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Relative directory to search (default '.').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max matches to return (default 40).",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "If true, ignore case (default false).",
                    },
                },
                required=["pattern"],
            ),
        ]

    tools = []
    if mode != "self_correct":
        tools.append(
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
            )
        )
    tools.extend(
        [
            _fn(
                "read_file",
                "Read a UTF-8 text file from the consumer repo by relative path."
                + (
                    " Paths must be on the repair path_allowlist."
                    if mode == "self_correct"
                    else ""
                ),
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
    )
    # Readonly search is useful for enrich + repair.
    tools.append(
        _fn(
            "grep",
            "Search file contents under the consumer repo (non-ignored paths). "
            "Returns matching lines with paths."
            + (
                " Prefer allowlisted / seeded paths."
                if mode == "self_correct"
                else ""
            ),
            {
                "pattern": {
                    "type": "string",
                    "description": "Literal or regex pattern to search for.",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional file glob, e.g. '**/*.py'.",
                },
                "directory": {
                    "type": "string",
                    "description": "Relative directory to search (default '.').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches to return (default 40).",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "If true, ignore case (default false).",
                },
            },
            required=["pattern"],
        )
    )
    if mode == "self_correct":
        tools.extend(
            [
                _fn(
                    "write_file",
                    "Write full UTF-8 contents to a relative path in the consumer repo. "
                    "Ignored oracle/contract paths are rejected. Prefer this over "
                    "remote/hosted sandboxes — only local writes affect the project.",
                    {
                        "path": {"type": "string"},
                        "contents": {"type": "string"},
                    },
                    required=["path", "contents"],
                ),
                _fn(
                    "run_tests",
                    "Run the consumer repo's test suite. Optionally pass pytest "
                    "nodeids (from failed_nodes) for a focused mid-repair retest.",
                    {
                        "nodeids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional pytest node ids, e.g. "
                                "tests/test_x.py::test_y. Omit to run the full suite."
                            ),
                        },
                    },
                ),
                _fn(
                    "run_shell",
                    "Run an allowlisted shell command in the consumer repo "
                    "(pytest, python -m pytest, python -c, pip show/list). "
                    "Arbitrary commands are rejected.",
                    {
                        "command": {
                            "type": "string",
                            "description": "Full command string to run.",
                        },
                    },
                    required=["command"],
                ),
            ]
        )
    return tools


def agent_tools(*, mode: ToolMode) -> list[dict[str, Any]]:
    """Built-ins + Conduit functions for a Responses agent turn."""
    if mode in {"anticheat_audit", "enrich_scoped"}:
        # Local inventory / audit only — no web_search / code_interpreter.
        return list(conduit_function_tools(mode=mode))
    return [*openai_builtin_tools(), *conduit_function_tools(mode=mode)]
