"""Run-start preflight: load consumer .env and warn about skipped pipeline steps."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conduit.credentials import llm_api_key, llm_provider


@dataclass(frozen=True)
class LlmStatus:
    provider: str | None
    available: bool
    model: str | None
    reason: str | None


def describe_llm_status() -> LlmStatus:
    """Report whether an LLM client can be built from the current environment."""
    from conduit.llm.client import get_llm_client, resolve_provider

    explicit = llm_provider()
    if explicit in {"none", "off", "disabled"}:
        return LlmStatus(
            provider=None,
            available=False,
            model=None,
            reason=f"CONDUIT_LLM_PROVIDER={explicit!r}",
        )

    resolved = resolve_provider()
    if not resolved:
        if explicit:
            return LlmStatus(
                provider=explicit,
                available=False,
                model=None,
                reason=f"CONDUIT_LLM_PROVIDER={explicit!r} but client could not be built",
            )
        return LlmStatus(
            provider=None,
            available=False,
            model=None,
            reason="no LLM provider or API key configured",
        )

    client = get_llm_client()
    if client is None:
        reason = _unavailable_reason(resolved)
        return LlmStatus(
            provider=resolved,
            available=False,
            model=None,
            reason=reason,
        )

    model = getattr(client, "model", None)
    return LlmStatus(
        provider=resolved,
        available=True,
        model=str(model) if model else None,
        reason=None,
    )


def _unavailable_reason(provider: str) -> str:
    if provider == "anthropic":
        if not llm_api_key():
            return "missing ANTHROPIC_API_KEY or CONDUIT_LLM_API_KEY"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return "anthropic package not installed"
        return "anthropic client could not be built"
    if provider == "custom":
        if not os.environ.get("CONDUIT_LLM_BASE_URL", "").strip():
            return "CONDUIT_LLM_BASE_URL is not set"
        return "custom LLM client could not be built"
    if provider == "openai":
        if not llm_api_key():
            return "missing OPENAI_API_KEY or CONDUIT_LLM_API_KEY"
        try:
            import openai  # noqa: F401
        except ImportError:
            return "openai package not installed"
        return "openai client could not be built"
    if provider == "ollama":
        try:
            import openai  # noqa: F401
        except ImportError:
            return "openai package not installed (required for Ollama client)"
        return "ollama client could not be built"
    return f"{provider} client could not be built"


def collect_run_preflight_warnings(
    root: Path,
    *,
    packet_file: Path | None = None,
    demo: bool = False,
    skip_modules: bool = False,
    skip_lockfile: bool = False,
    skip_export_delta: bool = False,
    skip_tests: bool = False,
    skip_pr: bool = False,
) -> list[str]:
    """Predictable skips and degraded modes knowable before detect runs."""
    _ = root
    warnings: list[str] = []

    if demo:
        warnings.append(
            "Demo mode (--demo): LLM client enrichment disabled; using offline fixtures"
        )

    if packet_file is not None:
        warnings.append(
            "Using published packet; vendor detect scrape skipped "
            "(detect signals will be empty)"
        )
        warnings.append(
            "LLM packet synthesis skipped (explicit --packet file)"
        )

    if skip_modules and packet_file is None:
        warnings.append(
            "Vendor detect modules skipped (--skip-modules); only lockfile/client scan runs"
        )

    if skip_lockfile:
        warnings.append("Lockfile diff skipped (--skip-lockfile)")

    if skip_export_delta:
        warnings.append("Export delta pruning skipped (--skip-export-delta)")

    if skip_tests:
        warnings.append(
            "Tests and self-correct skipped (--skip-tests); run the suite before merging"
        )

    if skip_pr:
        warnings.append("Pull request creation skipped (--skip-pr)")

    status = describe_llm_status()
    if demo:
        pass
    elif not status.available:
        reason = status.reason or "LLM not configured"
        warnings.append(
            f"LLM unavailable ({reason}): client enrichment, packet evidence "
            "enrichment, self-correct LLM, and LLM anti-cheat will be skipped or reduced"
        )
        if "missing OPENAI_API_KEY" in reason or "missing ANTHROPIC_API_KEY" in reason:
            warnings.append(
                "No cloud LLM API key found after .env load; verify may prompt later "
                "when tests run"
            )
    elif packet_file is None:
        warnings.append(
            f"LLM client enrichment will run when dossier has gaps "
            f"(provider={status.provider})"
        )

    return warnings


def print_run_preflight(
    console: Any,
    *,
    warnings: list[str],
    env_loaded: list[str] | None = None,
) -> None:
    """Print preflight info before the pulse spinner."""
    if env_loaded:
        names = ", ".join(sorted(set(env_loaded)))
        console.print(f"[dim]Loaded credentials from .env: {names}[/dim]")

    status = describe_llm_status()
    if status.available and status.provider:
        model_part = f" model={status.model}" if status.model else ""
        console.print(
            f"[dim]LLM: provider={status.provider}{model_part}[/dim]"
        )

    seen: set[str] = set()
    for warning in warnings:
        if warning in seen:
            continue
        seen.add(warning)
        console.print(f"[yellow]Warning:[/yellow] {warning}")
