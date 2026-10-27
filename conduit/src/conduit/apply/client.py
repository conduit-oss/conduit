"""Chat-completions HTTP client for Nebius Token Factory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ModelClient(Protocol):
    def chat(
        self,
        *,
        model: str,
        turns: list[ChatMessage],
        max_tokens: int = 2048,
        temperature: float = 0.2,
        enable_thinking: bool = False,
    ) -> ChatResult: ...


@dataclass
class TokenFactoryClient:
    """HTTP client for ``POST {base_url}chat/completions``.

    Uses the Token Factory chat-completions HTTP API. The API key is held in
    memory only and must never be printed or logged.

    Nemotron models may emit ``message.reasoning_content`` that counts against
    ``max_tokens``. We send ``chat_template_kwargs.enable_thinking`` (default
    false) and always read only ``message.content`` — never reasoning_content.
    """

    base_url: str
    api_key: str = field(repr=False)
    timeout: float = 120.0
    _http: httpx.Client | None = field(default=None, repr=False, compare=False)
    #: ``(model, enable_thinking) -> [calls, tokens]`` for answered calls, in call order.
    ledger: dict[tuple[str, bool], list[int]] = field(default_factory=dict, repr=False, compare=False)

    def endpoint(self) -> str:
        """The chat-completions URL as host and path only (no credentials or query)."""
        url = httpx.URL(self.base_url.rstrip("/") + "/chat/completions")
        return f"{url.scheme}://{url.host}{url.path}"

    def call_summary(self) -> list[dict[str, Any]]:
        """One entry per model and thinking setting: the endpoint, call count and tokens."""
        return [
            {"endpoint": self.endpoint(), "model": model, "enable_thinking": thinking, "calls": calls, "tokens": tokens}
            for (model, thinking), (calls, tokens) in self.ledger.items()
        ]

    def _client(self) -> httpx.Client:
        if self._http is None:
            # Do not put the key into default headers that might be repr'd.
            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def chat(
        self,
        *,
        model: str,
        turns: list[ChatMessage],
        max_tokens: int = 2048,
        temperature: float = 0.2,
        enable_thinking: bool = False,
    ) -> ChatResult:
        if not self.api_key:
            raise RuntimeError(
                "NEBIUS_API_KEY is not set. Export it to call Token Factory, "
                "or pass a fake client in tests."
            )
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in turns],
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Required for Nemotron: reasoning otherwise fills the token budget.
            "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = self._client().post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        # Intentionally ignore message.reasoning_content — it is not the fix.
        content = message.get("content") or ""
        usage = data.get("usage") or {}
        result = ChatResult(
            content=str(content),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or model),
        )
        entry = self.ledger.setdefault((model, bool(enable_thinking)), [0, 0])
        entry[0] += 1
        entry[1] += result.total_tokens
        return result


@dataclass
class FakeModelClient:
    """Deterministic client for offline unit tests.

    ``responses`` is a queue of strings (or callables taking turns).
    """

    responses: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def chat(
        self,
        *,
        model: str,
        turns: list[ChatMessage],
        max_tokens: int = 2048,
        temperature: float = 0.2,
        enable_thinking: bool = False,
    ) -> ChatResult:
        self.calls.append(
            {
                "model": model,
                "turns": turns,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "enable_thinking": enable_thinking,
            }
        )
        if not self.responses:
            raise RuntimeError("FakeModelClient has no more recorded responses")
        item = self.responses.pop(0)
        if callable(item):
            content = item(turns, model=model)
        else:
            content = str(item)
        # Approximate token use for cost-guard tests.
        prompt_chars = sum(len(m.content) for m in turns)
        return ChatResult(
            content=content,
            prompt_tokens=max(1, prompt_chars // 4),
            completion_tokens=max(1, len(content) // 4),
            model=model,
        )
