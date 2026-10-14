"""Sample external API client. The 1.2 wheel renames max_tokens."""

from acme_sdk import Client


def ask(prompt: str) -> str:
    client = Client()
    result = client.complete(prompt, max_tokens=32)
    return result["text"]
