"""Resolve and invoke chat LLMs without hardcoding a single vendor."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from conduit.llm.retry import call_with_rate_limit_retry
from conduit.llm.tools import resolve_max_turns, resolve_reasoning_effort
from conduit.pulse import beat, family_for_tool

ToolExecutor = Callable[[str, dict[str, Any]], str]


class LlmClient(Protocol):
    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        """Return a JSON object parsed from the model response."""

    def run_agent(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        """Agent loop with tools; final message parsed as JSON."""


def _parse_json_response(text: str) -> dict[str, Any]:
    from conduit.llm.json_util import extract_json_object

    return extract_json_object(text or "") or {}


def _item_type(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("type") or "")
    return str(getattr(item, "type", "") or "")


def _item_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        if _item_type(item) != "message":
            continue
        content = _item_get(item, "content") or []
        for block in content:
            btype = _item_type(block) if not isinstance(block, str) else ""
            if isinstance(block, dict):
                if block.get("type") in {"output_text", "text"} and block.get("text"):
                    parts.append(str(block["text"]))
            else:
                t = getattr(block, "text", None)
                if t and (not btype or btype in {"output_text", "text"}):
                    parts.append(str(t))
    return "\n".join(parts)


def _function_calls(response: Any) -> list[dict[str, Any]]:
    """Extract custom function_call items that need local execution."""
    out: list[dict[str, Any]] = []
    for item in getattr(response, "output", None) or []:
        if _item_type(item) != "function_call":
            continue
        name = str(_item_get(item, "name") or "")
        call_id = str(_item_get(item, "call_id") or _item_get(item, "id") or "")
        raw_args = _item_get(item, "arguments") or "{}"
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {"_raw": str(raw_args)}
        if not isinstance(args, dict):
            args = {"value": args}
        out.append({"name": name, "call_id": call_id, "arguments": args})
    return out


@dataclass
class _OpenAIResponsesClient:
    """OpenAI cloud client via /v1/responses (reasoning + tools)."""

    model: str
    api_key: str
    base_url: str | None = None
    reasoning_effort: str = "high"
    log: Callable[[str], None] | None = None
    _client: Any = field(default=None, repr=False, init=False)

    def _sdk(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": self.api_key or "local"}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def _create(self, **kwargs: Any) -> Any:
        create_kwargs = dict(kwargs)
        create_kwargs.setdefault("model", self.model)
        if self.reasoning_effort and self.reasoning_effort != "none":
            create_kwargs["reasoning"] = {"effort": self.reasoning_effort}

        def _do() -> Any:
            try:
                return self._sdk().responses.create(**create_kwargs)
            except Exception as exc:
                msg = str(exc).lower()
                if "reasoning" in msg and ("unsupported" in msg or "unknown" in msg):
                    create_kwargs.pop("reasoning", None)
                    return self._sdk().responses.create(**create_kwargs)
                if "code_interpreter" in msg and "container" in msg:
                    tools = create_kwargs.get("tools")
                    if isinstance(tools, list):
                        create_kwargs["tools"] = [
                            (
                                {"type": "code_interpreter"}
                                if isinstance(t, dict)
                                and t.get("type") == "code_interpreter"
                                else t
                            )
                            for t in tools
                        ]
                        return self._sdk().responses.create(**create_kwargs)
                raise

        return call_with_rate_limit_retry(_do, log=self.log)

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        beat("think")
        resp = self._create(
            input=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": user + "\n\nReply with a single JSON object only.",
                },
            ],
        )
        return _parse_json_response(_output_text(resp))

    def run_agent(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        turns = resolve_max_turns(32) if max_turns is None else max(1, max_turns)
        tool_list = list(tools or [])
        input_items: list[Any] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    user
                    + "\n\nWhen finished, reply with a single JSON object only "
                    "(no markdown fences)."
                ),
            },
        ]
        previous_id: str | None = None
        last_text = ""
        emit = self.log

        for turn_idx in range(turns):
            beat("think")
            if emit is not None:
                emit(f"[llm] turn {turn_idx + 1}/{turns}")
            create_kwargs: dict[str, Any] = {}
            if previous_id:
                create_kwargs["previous_response_id"] = previous_id
                create_kwargs["input"] = input_items
            else:
                create_kwargs["input"] = input_items

            if tool_list:
                create_kwargs["tools"] = tool_list
                create_kwargs["tool_choice"] = "auto"

            resp = self._create(**create_kwargs)
            previous_id = getattr(resp, "id", None) or previous_id
            last_text = _output_text(resp)
            calls = _function_calls(resp)
            if not calls:
                return _parse_json_response(last_text)

            if tool_executor is None:
                return _parse_json_response(last_text) or {
                    "error": "model requested tools but no executor was provided",
                    "calls": [c["name"] for c in calls],
                }

            names = [str(c.get("name") or "?") for c in calls]
            if emit is not None:
                emit(f"[llm] tools: {', '.join(names)}")

            input_items = []
            for call in calls:
                beat(family_for_tool(call["name"]))
                output = tool_executor(call["name"], call["arguments"])
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": output
                        if isinstance(output, str)
                        else json.dumps(output),
                    }
                )

        if emit is not None:
            emit(f"[llm] exceeded max_turns ({turns})")
        return _parse_json_response(last_text) or {
            "error": "agent exceeded max_turns without a final JSON answer",
        }


@dataclass
class _ChatCompletionsClient:
    """OpenAI-compatible Chat Completions (Ollama, vLLM, LM Studio, etc.)."""

    model: str
    api_key: str
    base_url: str | None = None
    use_json_mode: bool = False

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        beat("think")
        from openai import OpenAI

        kwargs: dict[str, Any] = {"api_key": self.api_key or "local"}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)

        def _do() -> Any:
            create_kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
            }
            if self.use_json_mode:
                create_kwargs["response_format"] = {"type": "json_object"}
            return client.chat.completions.create(**create_kwargs)

        resp = call_with_rate_limit_retry(_do)
        content = resp.choices[0].message.content or "{}"
        return _parse_json_response(content)

    def run_agent(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        # Local OpenAI-compatible servers: no Responses built-ins; one-shot JSON.
        _ = tools, tool_executor, max_turns
        return self.complete_json(system=system, user=user)


@dataclass
class _AnthropicClient:
    model: str
    api_key: str

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        beat("think")
        from anthropic import Anthropic

        client = Anthropic(api_key=self.api_key)

        def _do() -> Any:
            return client.messages.create(
                model=self.model,
                max_tokens=8192,
                system=system + "\nReply with a single JSON object only.",
                messages=[{"role": "user", "content": user}],
                temperature=0,
            )

        resp = call_with_rate_limit_retry(_do)
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return _parse_json_response("\n".join(parts))

    def run_agent(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        _ = tools, tool_executor, max_turns
        return self.complete_json(system=system, user=user)


def _normalize_provider(name: str) -> str:
    if name in {"openai_compatible", "compatible"}:
        return "custom"
    return name


def resolve_provider() -> str | None:
    """Return provider name or None if no LLM is configured."""
    explicit = _normalize_provider(
        os.environ.get("CONDUIT_LLM_PROVIDER", "").strip().lower()
    )
    if explicit in {"openai", "anthropic", "ollama", "custom"}:
        return explicit
    if explicit in {"none", "off", "disabled"}:
        return None
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai"
    if os.environ.get("CONDUIT_LLM_BASE_URL", "").strip():
        return "custom"
    return None


def _api_key_for(provider: str) -> str:
    generic = os.environ.get("CONDUIT_LLM_API_KEY", "").strip()
    if generic:
        return generic
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if provider == "openai":
        return os.environ.get("OPENAI_API_KEY", "").strip()
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _default_model(provider: str) -> str:
    override = os.environ.get("CONDUIT_LLM_MODEL", "").strip()
    if override:
        return override
    if provider == "anthropic":
        return "claude-3-5-haiku-latest"
    if provider in {"ollama", "custom"}:
        return "llama3.2"
    return "gpt-5.4-mini"


def get_llm_client(
    *, log: Callable[[str], None] | None = None
) -> LlmClient | None:
    """Build a client from env, or None when LLM use should be skipped."""
    provider = resolve_provider()
    if not provider:
        return None

    model = _default_model(provider)
    api_key = _api_key_for(provider)
    base_url = os.environ.get("CONDUIT_LLM_BASE_URL", "").strip() or None
    effort = resolve_reasoning_effort("high")

    if provider == "anthropic":
        if not api_key:
            return None
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return None
        return _AnthropicClient(model=model, api_key=api_key)

    try:
        import openai  # noqa: F401
    except ImportError:
        return None

    if provider == "ollama":
        return _ChatCompletionsClient(
            model=model,
            api_key=api_key or "ollama",
            base_url=base_url or "http://127.0.0.1:11434/v1",
            use_json_mode=False,
        )

    if provider == "custom":
        if not base_url:
            return None
        return _ChatCompletionsClient(
            model=model,
            api_key=api_key or "local",
            base_url=base_url,
            use_json_mode=False,
        )

    if not api_key:
        return None
    return _OpenAIResponsesClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        reasoning_effort=effort,
        log=log,
    )
