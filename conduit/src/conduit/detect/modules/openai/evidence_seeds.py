"""OpenAI evidence seeds (URLs + host allowlist) for LLM packet synthesis.

Info-point: update when OpenAI moves canonical docs. No path/call invent maps.
"""

from __future__ import annotations

OPENAI_EVIDENCE_SEEDS: list[str] = [
    "https://platform.openai.com/docs/deprecations",
    "https://developers.openai.com/api/docs/deprecations",
    "https://developers.openai.com/api/docs/models",
    "https://developers.openai.com/api/docs/models.md",
    "https://platform.openai.com/docs/changelog",
    "https://github.com/openai/openai-python/discussions/742",
    "https://github.com/openai/openai-python/discussions/",
    "https://github.com/openai/openai-python/blob/main/README.md",
    "https://github.com/openai/"
]

OPENAI_EVIDENCE_HOSTS: frozenset[str] = frozenset(
    {
        "platform.openai.com",
        "developers.openai.com",
        "github.com",
    }
)


def openai_evidence_queries(from_version: str, to_version: str) -> list[str]:
    return [
        f"openai python SDK migration {from_version} to {to_version}",
        "openai API deprecations endpoint replacement",
        "openai chat completions max_tokens max_completion_tokens",
        "openai model supported endpoints chat completions",
        "site:developers.openai.com/api/docs/models supported endpoints",
    ]
