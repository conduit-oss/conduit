"""Class wrapper around the legacy OpenAI SDK."""

from __future__ import annotations

import openai


class Gateway:
    def complete(self, prompt: str) -> str:
        response = openai.ChatCompletion.create(
            model="gpt-4-0613",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=32,
        )
        return response["choices"][0]["message"]["content"] or ""


def ask_gateway(prompt: str) -> str:
    return Gateway().complete(prompt)
