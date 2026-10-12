"""Calls ChatCompletion through a package re-export, not an openai import."""

from __future__ import annotations

from pkg import ChatCompletion


def legacy(prompt: str) -> str:
    response = ChatCompletion.create(
        model="gpt-4-0613",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8,
    )
    return response["choices"][0]["message"]["content"] or ""
