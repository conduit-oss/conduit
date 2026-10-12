"""Raw REST to the OpenAI API. No SDK import."""

from __future__ import annotations

import requests

API = "https://api.openai.com/v1/chat/completions"


def post_chat(prompt: str) -> str:
    response = requests.post(API, json={"model": "gpt-4-0613", "messages": []})
    return response.text


def ask_http(prompt: str) -> str:
    return post_chat(prompt)
