"""Single-function wrapper around the legacy OpenAI SDK."""

from __future__ import annotations

import openai


def complete(prompt: str, max_tokens: int = 64) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-4-0613",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return response["choices"][0]["message"]["content"] or ""
