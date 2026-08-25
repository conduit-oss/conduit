"""Azure bridge module template for impact-driven post-rules."""

from __future__ import annotations


def azure_bridge_module_body() -> str:
    """Oracle-safe AzureOpenAI bridge (no legacy api_type globals)."""
    return '''"""Azure OpenAI bridge using the AzureOpenAI client."""

from __future__ import annotations

import os

from openai import AzureOpenAI

from helix_common import DEFAULT_COMPLETION_MODEL


def _client() -> AzureOpenAI:
    key = os.environ.get("AZURE_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY")
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not key or not endpoint:
        raise ValueError("AZURE_OPENAI_KEY and AZURE_OPENAI_ENDPOINT are required")
    return AzureOpenAI(
        api_key=key,
        azure_endpoint=endpoint.rstrip("/"),
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    )


def configure_azure():
    """Return Azure client configuration metadata."""
    _client()
    return {
        "api_type": "azure",
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    }


def complete_on_azure(prompt, deployment=None, max_tokens=32):
    """Completion against an Azure deployment."""
    client = _client()
    model = deployment or os.environ.get(
        "AZURE_OPENAI_DEPLOYMENT",
        DEFAULT_COMPLETION_MODEL,
    )
    response = client.completions.create(
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
    )
    choice = response.choices[0]
    text = getattr(choice, "text", None)
    if text is not None:
        return str(text)
    return str(choice)


def chat_on_azure(message, deployment=None):
    """Chat completion on an Azure deployment."""
    client = _client()
    model = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.6-sol")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": message}],
        max_completion_tokens=64,
    )
    choice = response.choices[0]
    msg = choice.message
    content = getattr(msg, "content", None) if msg is not None else None
    return str(content or "")
'''
