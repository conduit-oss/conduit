"""Demo consumer stuck on the openai 0.28 ChatCompletion API."""

from __future__ import annotations

import openai

DEFAULT_MODEL = "gpt-4-0613"


def complete(prompt: str, model: str = DEFAULT_MODEL) -> str:
    response = openai.ChatCompletion.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=64,
    )
    return response["choices"][0]["message"]["content"] or ""


if __name__ == "__main__":
    print(f"Using model {DEFAULT_MODEL}")
