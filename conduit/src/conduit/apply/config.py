"""Configurable Token Factory endpoint and model ids.

Defaults follow Nebius Token Factory docs:
https://docs.tokenfactory.nebius.com/api-reference/introduction
https://tokenfactory.nebius.com/model-catalog.md
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Shared chat-completions gateway (Token Factory intro / switch docs).
DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
# Large model for proposing function-scoped diffs (catalog: Nemotron-3-Ultra).
DEFAULT_MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"
# Small/fast model for cheap checks and one-line summaries (catalog: Nano).
DEFAULT_MODEL_SMALL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"

ENV_API_KEY = "NEBIUS_API_KEY"
ENV_BASE_URL = "CONDUIT_APPLY_BASE_URL"
ENV_MODEL = "CONDUIT_APPLY_MODEL"
ENV_MODEL_SMALL = "CONDUIT_APPLY_MODEL_SMALL"
ENV_MAX_TOKENS_PER_SITE = "CONDUIT_APPLY_MAX_TOKENS_PER_SITE"
ENV_ENABLE_THINKING = "CONDUIT_APPLY_ENABLE_THINKING"
ENV_ENABLE_THINKING_SMALL = "CONDUIT_APPLY_ENABLE_THINKING_SMALL"

DEFAULT_MAX_TOKENS_PER_SITE = 8_000
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class TokenFactorySettings:
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    model_small: str = ""
    max_tokens_per_site: int = DEFAULT_MAX_TOKENS_PER_SITE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: When False (default), send chat_template_kwargs.enable_thinking=false
    #: on propose calls so Nemotron does not spend max_tokens on reasoning.
    enable_thinking: bool = False
    enable_thinking_small: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str | None = None,
        model: str | None = None,
        model_small: str | None = None,
        max_tokens_per_site: int | None = None,
        max_attempts: int | None = None,
        api_key: str | None = None,
        enable_thinking: bool | None = None,
        enable_thinking_small: bool | None = None,
    ) -> TokenFactorySettings:
        key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
        # Never echo the key; callers must not log it.
        return cls(
            base_url=(base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
            + "/",
            api_key=key,
            model=model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL,
            model_small=model_small or os.environ.get(ENV_MODEL_SMALL) or DEFAULT_MODEL_SMALL,
            max_tokens_per_site=max_tokens_per_site
            if max_tokens_per_site is not None
            else _env_int(ENV_MAX_TOKENS_PER_SITE, DEFAULT_MAX_TOKENS_PER_SITE),
            max_attempts=max_attempts if max_attempts is not None else DEFAULT_MAX_ATTEMPTS,
            enable_thinking=_resolve_bool(
                enable_thinking, ENV_ENABLE_THINKING, default=False
            ),
            enable_thinking_small=_resolve_bool(
                enable_thinking_small, ENV_ENABLE_THINKING_SMALL, default=False
            ),
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _resolve_bool(explicit: bool | None, env_name: str, *, default: bool) -> bool:
    if explicit is not None:
        return bool(explicit)
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
