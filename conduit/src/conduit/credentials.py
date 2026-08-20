"""Require or prompt for API keys before consumer verify.

Skipped live tests must never count as a pass. When the consumer (or packet)
needs OpenAI, Conduit either exports a key from the environment, prompts on a
TTY, or exits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

import typer

from conduit.prune.grep_imports import SKIP_DIRS

PromptFn = Callable[[str], str]


class CredentialsError(RuntimeError):
    """Missing credentials and no TTY (or the user declined)."""


def prompt_secret(label: str) -> str:
    """Interactive hidden prompt. Tests monkeypatch this."""
    return str(typer.prompt(label, hide_input=True)).strip()


def openai_consumer_key() -> str:
    return (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENAI_KEY", "").strip()
    )


def llm_api_key() -> str:
    return (
        os.environ.get("CONDUIT_LLM_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


def llm_provider() -> str:
    return os.environ.get("CONDUIT_LLM_PROVIDER", "").strip().lower()


def llm_needs_cloud_key() -> bool:
    provider = llm_provider()
    if provider in {"none", "off", "disabled"}:
        return False
    if provider in {"ollama", "custom"}:
        return False
    if provider in {"openai", "anthropic"}:
        return True
    # Auto-detect path (provider unset): a cloud key is only required when we
    # actually intend to call a cloud LLM. Callers pass ``want_llm``.
    return provider == ""


def packet_is_openai(packet: dict[str, Any] | None) -> bool:
    pkg = str((packet or {}).get("package") or "").strip().lower()
    return pkg == "openai"


def consumer_mentions_openai_key(root: Path) -> bool:
    """True if tests/conftest skip or document OPENAI_API_KEY."""
    root = root.resolve()
    needles = ("OPENAI_API_KEY", "OPENAI_KEY")
    candidates: list[Path] = [root / "conftest.py", root / "tests" / "conftest.py"]
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        candidates.extend(tests_dir.rglob("test_*.py"))
        candidates.extend(tests_dir.rglob("conftest.py"))
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        if any(part in SKIP_DIRS for part in resolved.parts):
            continue
        seen.add(resolved)
        try:
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(n in text for n in needles):
            return True
    return False


def needs_openai_consumer_key(root: Path, packet: dict[str, Any] | None) -> bool:
    return packet_is_openai(packet) or consumer_mentions_openai_key(root)


def _export_openai_key(value: str) -> None:
    os.environ["OPENAI_API_KEY"] = value
    if not os.environ.get("OPENAI_KEY", "").strip():
        os.environ["OPENAI_KEY"] = value


def _prompt_or_fail(label: str, *, interactive: bool, prompt: PromptFn) -> str:
    if not interactive:
        raise CredentialsError(
            f"{label} is not set. Export it or re-run from a TTY to be prompted."
        )
    value = prompt(label).strip()
    if not value:
        raise CredentialsError(f"{label} is required.")
    return value


def ensure_verify_credentials(
    root: Path,
    packet: dict[str, Any] | None,
    *,
    want_llm: bool = False,
    interactive: bool | None = None,
    prompt: PromptFn | None = None,
) -> None:
    """Ensure consumer (and optionally LLM) keys exist, prompting on a TTY."""
    prompt = prompt or prompt_secret
    if interactive is None:
        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    if needs_openai_consumer_key(root, packet) and not openai_consumer_key():
        value = _prompt_or_fail(
            "OPENAI_API_KEY",
            interactive=interactive,
            prompt=prompt,
        )
        _export_openai_key(value)

    if not want_llm:
        return
    provider = llm_provider()
    if provider in {"none", "off", "disabled"}:
        return
    if provider in {"ollama", "custom"}:
        return
    if provider == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get(
            "CONDUIT_LLM_API_KEY", ""
        ).strip():
            return
        value = _prompt_or_fail(
            "ANTHROPIC_API_KEY (or CONDUIT_LLM_API_KEY)",
            interactive=interactive,
            prompt=prompt,
        )
        os.environ["ANTHROPIC_API_KEY"] = value
        return
    # openai or auto-detect
    if llm_api_key():
        return
    value = _prompt_or_fail(
        "OPENAI_API_KEY (or CONDUIT_LLM_API_KEY)",
        interactive=interactive,
        prompt=prompt,
    )
    if not os.environ.get("CONDUIT_LLM_API_KEY", "").strip():
        os.environ["CONDUIT_LLM_API_KEY"] = value
    if not openai_consumer_key():
        _export_openai_key(value)
