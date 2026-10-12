"""Implementation hidden behind the package re-export."""

from __future__ import annotations

import openai


def complete_chat(prompt: str) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-4-0613",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8,
    )
    return response["choices"][0]["message"]["content"] or ""
