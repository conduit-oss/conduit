"""Application call sites. This module does not import openai."""

from __future__ import annotations

from gateway import Gateway
from llm import complete


def ask(prompt: str) -> str:
    return complete(prompt, max_tokens=16)


def ask_via_class(prompt: str) -> str:
    client = Gateway()
    return client.complete(prompt)


def main(prompt: str) -> str:
    return ask(prompt)
