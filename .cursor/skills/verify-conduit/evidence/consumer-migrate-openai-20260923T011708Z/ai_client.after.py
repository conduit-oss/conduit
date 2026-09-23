"""Demo consumer stuck on the openai 0.28 ChatCompletion API."""

from __future__ import annotations

import openai
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DEFAULT_MODEL = "gpt-4o"


def complete(prompt: str, model: str = DEFAULT_MODEL) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=64,
    )
    return response.choices[0].message.content or ""


if __name__ == "__main__":
    print(f"Using model {DEFAULT_MODEL}")
