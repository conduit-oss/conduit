"""Require or prompt for API keys before consumer verify.

Skipped live tests must never count as a pass. When the consumer (or packet)
needs OpenAI, Conduit loads ``.env``, exports a key from the environment,
prompts on a TTY, or exits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

import typer

from conduit.prune.grep_imports import SKIP_DIRS

PromptFn = Callable[[str], str]
LogFn = Callable[[str], None]

_CREDENTIAL_ENV_FILES = (".env", ".env.local")


class CredentialsError(RuntimeError):
    """Missing credentials and no TTY (or the user declined)."""


def prompt_secret(label: str) -> str:
    """Interactive hidden prompt. Tests monkeypatch this."""
    return str(typer.prompt(label, hide_input=True)).strip()


def _parse_env_line(line: str) -> tuple[str, str] | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith("export "):
        raw = raw[7:].strip()
    if "=" not in raw:
        return None
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


def load_consumer_env(root: Path) -> list[str]:
    """
    Load ``.env`` / ``.env.local`` from the consumer repo into ``os.environ``.

    Existing exported variables win. Returns env var names newly set (no values).
    """
    root = root.resolve()
    loaded: list[str] = []
    for name in _CREDENTIAL_ENV_FILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            parsed = _parse_env_line(line)
            if not parsed:
                continue
            key, value = parsed
            if os.environ.get(key, "").strip():
                continue
            os.environ[key] = value
            loaded.append(key)
    if (
        not os.environ.get("OPENAI_API_KEY", "").strip()
        and os.environ.get("OPENAI_KEY", "").strip()
    ):
        os.environ["OPENAI_API_KEY"] = os.environ["OPENAI_KEY"].strip()
        if "OPENAI_API_KEY" not in loaded:
            loaded.append("OPENAI_API_KEY")
    return loaded


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


def _prompt_or_fail(
    label: str,
    *,
    interactive: bool,
    prompt: PromptFn,
    log: LogFn | None = None,
    console: Any | None = None,
) -> str:
    if not interactive:
        raise CredentialsError(
            f"{label} is not set. Export it, add to .env, or re-run from a TTY "
            "to be prompted."
        )
    from conduit.pulse import pause_pulse, resume_pulse

    pause_pulse()
    if log:
        log(
            f"[yellow]{label} is not set (checked environment and .env).[/yellow]\n"
            "Enter API key (input hidden), or Ctrl+C to abort:"
        )
    try:
        value = prompt(label).strip()
        if not value:
            raise CredentialsError(f"{label} is required.")
        return value
    finally:
        if console is not None:
            resume_pulse(console)


def ensure_verify_credentials(
    root: Path,
    packet: dict[str, Any] | None,
    *,
    want_llm: bool = False,
    interactive: bool | None = None,
    prompt: PromptFn | None = None,
    log: LogFn | None = None,
    console: Any | None = None,
    demo: bool = False,
) -> None:
    """Ensure consumer (and optionally LLM) keys exist, prompting on a TTY.

    When ``demo=True``, skip the consumer ``OPENAI_API_KEY`` gate (offline
    fixtures / sample consumer). LLM credentials are still required if
    ``want_llm`` is set.
    """
    prompt = prompt or prompt_secret
    if interactive is None:
        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    loaded = load_consumer_env(root)
    if loaded and log:
        names = ", ".join(sorted(set(loaded)))
        log(f"[dim]Loaded credentials from .env: {names}[/dim]")

    need_consumer_openai = needs_openai_consumer_key(root, packet) and not demo
    if need_consumer_openai and not openai_consumer_key():
        value = _prompt_or_fail(
            "OPENAI_API_KEY",
            interactive=interactive,
            prompt=prompt,
            log=log,
            console=console,
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
            log=log,
            console=console,
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
        log=log,
        console=console,
    )
    if not os.environ.get("CONDUIT_LLM_API_KEY", "").strip():
        os.environ["CONDUIT_LLM_API_KEY"] = value
    if not openai_consumer_key():
        _export_openai_key(value)
