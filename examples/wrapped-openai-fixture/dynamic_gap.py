"""getattr is intentionally invisible to the static graph."""

from __future__ import annotations

import openai


def hidden(method: str):
    return getattr(openai, method).create()
