"""Map OpenAI REST paths / client api_patterns to SDK call targets for param renames."""

from __future__ import annotations

import re
from typing import Iterable

# Grounded OpenAI REST path → modern Python SDK callee surface.
_PATH_TO_CALLEES: dict[str, list[str]] = {
    "/v1/chat/completions": [
        "chat.completions.create",
        "openai.chat.completions.create",
    ],
    "/v1/completions": [
        "completions.create",
        "openai.completions.create",
        "Completion.create",
    ],
    "/v1/embeddings": [
        "embeddings.create",
        "openai.embeddings.create",
    ],
    "/v1/images/generations": [
        "images.generate",
        "openai.images.generate",
    ],
    "/v1/images/edits": [
        "images.edit",
        "openai.images.edit",
    ],
    "/v1/audio/transcriptions": [
        "audio.transcriptions.create",
        "openai.audio.transcriptions.create",
    ],
    "/v1/audio/translations": [
        "audio.translations.create",
        "openai.audio.translations.create",
    ],
    "/v1/audio/speech": [
        "audio.speech.create",
        "openai.audio.speech.create",
    ],
    "/v1/moderations": [
        "moderations.create",
        "openai.moderations.create",
    ],
    "/v1/responses": [
        "responses.create",
        "openai.responses.create",
    ],
}

_API_PATTERN_TO_PATH: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^chat\.completions$", re.I), "/v1/chat/completions"),
    (re.compile(r"^ChatCompletion$", re.I), "/v1/chat/completions"),
    (re.compile(r"^embeddings\.create$", re.I), "/v1/embeddings"),
    (re.compile(r"^Completion\.create$", re.I), "/v1/completions"),
]

_PATH_RE = re.compile(r"^/v1/[A-Za-z0-9/_\-{}]+$")


def normalize_api_path(path: str | None) -> str | None:
    if not path:
        return None
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    if not _PATH_RE.match(p):
        return None
    return p


def looks_like_api_path(value: str | None) -> bool:
    return normalize_api_path(value) is not None


def path_for_api_pattern(token: str) -> str | None:
    t = (token or "").strip()
    if not t:
        return None
    if t.startswith("/v1/") or t.startswith("v1/"):
        return normalize_api_path(t if t.startswith("/") else f"/{t}")
    for pattern, path in _API_PATTERN_TO_PATH:
        if pattern.match(t):
            return path
    return None


def callees_for_path(
    path: str | None,
    *,
    api_patterns: Iterable[str] | None = None,
) -> list[str]:
    """Return SDK function_target candidates for a REST path."""
    norm = normalize_api_path(path)
    if not norm:
        return []

    out: list[str] = []
    seen: set[str] = set()

    # Prefer client-observed patterns that map to this path.
    for raw in api_patterns or []:
        token = str(raw or "").strip()
        if not token:
            continue
        mapped = path_for_api_pattern(token)
        if mapped != norm:
            continue
        # Promote bare tokens to .create when needed
        if token.endswith(".create") or token.endswith(".generate") or token.endswith(".edit"):
            candidate = token
        elif token.lower() == "chat.completions":
            candidate = "chat.completions.create"
        elif token == "ChatCompletion":
            candidate = "ChatCompletion.create"
        elif token == "Completion.create":
            candidate = "Completion.create"
        else:
            candidate = token
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    for candidate in _PATH_TO_CALLEES.get(norm, []):
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    return out
