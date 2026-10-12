"""`from openai import OpenAI as C`, then a helper that returns the client."""

from __future__ import annotations

from openai import OpenAI as C


def build():
    return C()


def ask_direct(prompt: str) -> str:
    client = C()
    response = client.chat.completions.create(
        model="gpt-4-0613",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8,
    )
    return response.choices[0].message.content or ""


def ask_aliased(prompt: str) -> str:
    client = build()
    response = client.chat.completions.create(
        model="gpt-4-0613",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8,
    )
    return response.choices[0].message.content or ""
