"""Map REST paths / client api_patterns to SDK call targets for param renames."""

from __future__ import annotations

import re
from typing import Iterable

from conduit.detect.vendor_profile import VendorProfile

# Kept for tests / callers that don't pass a profile; OPENAI_PROFILE mirrors these.
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
    "/v1/edits": [
        "Edit.create",
        "openai.Edit.create",
    ],
    "/v1/engines": [
        "Engine.list",
        "openai.Engine.list",
    ],
    "/v1/fine-tunes": [
        "FineTune.list",
        "openai.FineTune.list",
        "FineTune.create",
        "openai.FineTune.create",
    ],
    "/v1/fine_tuning/jobs": [
        "fine_tuning.jobs.create",
        "openai.fine_tuning.jobs.create",
        "fine_tuning.jobs.list",
        "openai.fine_tuning.jobs.list",
    ],
    "/v1/models": [
        "models.list",
        "openai.models.list",
        "Engine.list",
        "openai.Engine.list",
    ],
    "/v1/images/generations": [
        "images.generate",
        "openai.images.generate",
        "Image.create",
        "openai.Image.create",
    ],
    "/v1/embeddings": [
        "embeddings.create",
        "openai.embeddings.create",
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
        "Moderation.create",
        "openai.Moderation.create",
    ],
    "/v1/responses": [
        "responses.create",
        "openai.responses.create",
    ],
}

_API_PATTERN_TO_PATH: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^chat\.completions(?:\.create)?$", re.I), "/v1/chat/completions"),
    (re.compile(r"^ChatCompletion(?:\.create)?$", re.I), "/v1/chat/completions"),
    (re.compile(r"^(?:openai\.)?embeddings\.create$", re.I), "/v1/embeddings"),
    (re.compile(r"^(?:openai\.)?Completion\.create$", re.I), "/v1/completions"),
    (re.compile(r"^(?:openai\.)?Edit\.create$", re.I), "/v1/edits"),
    (re.compile(r"^(?:openai\.)?Engine(?:\.list|\.retrieve)?$", re.I), "/v1/engines"),
    (re.compile(r"^(?:openai\.)?FineTune(?:\.list|\.create)?$", re.I), "/v1/fine-tunes"),
    (re.compile(r"^(?:openai\.)?Image\.(?:create|create_edit)$", re.I), "/v1/images/generations"),
    (re.compile(r"^(?:openai\.)?Moderation\.create$", re.I), "/v1/moderations"),
]

_PATH_RE = re.compile(r"^/v1/[A-Za-z0-9/_\-{}]+$")


def _active_profile(profile: VendorProfile | None) -> VendorProfile | None:
    if profile is not None:
        return profile
    try:
        from conduit.detect.modules.openai.profile import OPENAI_PROFILE

        return OPENAI_PROFILE
    except Exception:
        return None


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


def path_for_api_pattern(
    token: str, *, profile: VendorProfile | None = None
) -> str | None:
    t = (token or "").strip()
    if not t:
        return None
    if t.startswith("/v1/") or t.startswith("v1/"):
        return normalize_api_path(t if t.startswith("/") else f"/{t}")
    prof = _active_profile(profile)
    pairs = (
        [(re.compile(pat, re.I), path) for pat, path in prof.api_pattern_to_path]
        if prof and prof.api_pattern_to_path
        else _API_PATTERN_TO_PATH
    )
    for pattern, path in pairs:
        if pattern.match(t):
            return path
    return None


def modern_callees_for_path(
    path: str | None,
    *,
    profile: VendorProfile | None = None,
) -> list[str]:
    """Return structural path_to_callees entries only (no client api_patterns)."""
    norm = normalize_api_path(path)
    if not norm:
        return []
    prof = _active_profile(profile)
    table = (prof.path_to_callees if prof and prof.path_to_callees else _PATH_TO_CALLEES)
    return list(table.get(norm, []))


def callees_for_path(
    path: str | None,
    *,
    api_patterns: Iterable[str] | None = None,
    profile: VendorProfile | None = None,
) -> list[str]:
    """Return SDK function_target candidates for a REST path."""
    norm = normalize_api_path(path)
    if not norm:
        return []

    out: list[str] = []
    seen: set[str] = set()
    prof = _active_profile(profile)

    # Prefer client-observed patterns that map to this path.
    for raw in api_patterns or []:
        token = str(raw or "").strip()
        if not token or looks_like_api_path(token):
            continue
        mapped = path_for_api_pattern(token, profile=prof)
        if mapped != norm:
            continue
        if token.endswith(".create") or token.endswith(".generate") or token.endswith(".edit"):
            candidate = token
        elif token.endswith(".list"):
            candidate = token
        elif token.lower() == "chat.completions":
            candidate = "chat.completions.create"
        elif token == "ChatCompletion":
            candidate = "ChatCompletion.create"
        elif token == "Completion.create":
            candidate = "Completion.create"
        else:
            candidate = token
        if looks_like_api_path(candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    for candidate in modern_callees_for_path(norm, profile=prof):
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    return out
