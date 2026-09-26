"""Legacy google-generativeai usage for Conduit packet dry-run."""

from __future__ import annotations

import google.generativeai as genai
from google.generativeai import GenerationConfig


def configure(api_key: str) -> None:
    genai.configure(api_key=api_key)


def complete(prompt: str, model_name: str = "gemini-1.5-flash") -> str:
    model = genai.GenerativeModel(
        model_name,
        generation_config=GenerationConfig(
            max_output_tokens=64,
            temperature=0.2,
        ),
    )
    response = model.generate_content(prompt)
    return response.text or ""


def embed(text: str) -> object:
    return genai.embed_content(
        model="models/gemini-embedding-001",
        content=text,
    )


def upload(path: str) -> object:
    return genai.upload_file(path=path)
