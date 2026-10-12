"""Calls the re-exported helper. Does not import openai."""

from __future__ import annotations

from pkg import complete_chat


def run(prompt: str) -> str:
    return complete_chat(prompt)
