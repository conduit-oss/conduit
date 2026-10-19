"""Propose → validate → verify loop for each impact site."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conduit.apply.client import ChatMessage, ModelClient
from conduit.apply.config import TokenFactorySettings
from conduit.apply.context import ContextError, build_context_pack
from conduit.apply.models import ContextPack, ImpactSite, SiteResult, SiteStatus
from conduit.apply.patch import (
    PatchError,
    apply_unified_diff,
    ensure_parses,
    extract_unified_diff,
    validate_diff_scope,
)
from conduit.apply.prompts import propose_messages, summarize_messages

# Optional hook: given root + site + patched file text, return True if the site
# is no longer flagged. Unit tests inject this; live runs may re-check impact.
SiteClearedCheck = Callable[[Path, ImpactSite, str], bool]


@dataclass
class ApplyConfig:
    root: Path
    settings: TokenFactorySettings
    dry_run: bool = False
    write: bool = False
    test_cmd: str | None = None
    client: ModelClient | None = None
    site_cleared: SiteClearedCheck | None = None
    max_sites: int | None = None


@dataclass
class ApplyReport:
    results: list[SiteResult] = field(default_factory=list)
    tokens_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
            "tokens_used": self.tokens_used,
            "summary": {
                status.value: sum(1 for r in self.results if r.status == status)
                for status in SiteStatus
            },
        }


def apply_sites(sites: list[ImpactSite], config: ApplyConfig) -> ApplyReport:
    report = ApplyReport()
    client = config.client
    owns_client = False
    if client is None and not config.dry_run:
        from conduit.apply.client import TokenFactoryClient

        client = TokenFactoryClient(
            base_url=config.settings.base_url,
            api_key=config.settings.api_key,
        )
        owns_client = True
    try:
        selected = sites if config.max_sites is None else sites[: config.max_sites]
        for site in selected:
            report.results.append(_apply_one(site, config, client, report))
    finally:
        if owns_client and client is not None and hasattr(client, "close"):
            client.close()  # type: ignore[union-attr]
    return report


def _apply_one(
    site: ImpactSite,
    config: ApplyConfig,
    client: ModelClient | None,
    report: ApplyReport,
) -> SiteResult:
    try:
        pack = build_context_pack(config.root, site)
    except ContextError as exc:
        return SiteResult(site=site, status=SiteStatus.NEEDS_HUMAN, error=str(exc))

    if config.dry_run:
        return SiteResult(
            site=site,
            status=SiteStatus.DRY_RUN,
            why="context pack only (--dry-run); model not called",
            context=pack,
        )

    if client is None:
        return SiteResult(
            site=site,
            status=SiteStatus.NEEDS_HUMAN,
            error="no model client available",
            context=pack,
        )

    feedback = ""
    last_error = ""
    last_diff = ""
    attempts = 0
    site_tokens = 0
    for attempt in range(1, config.settings.max_attempts + 1):
        attempts = attempt
        remaining = config.settings.max_tokens_per_site - site_tokens
        if remaining <= 0:
            last_error = "token budget exhausted for this site"
            break
        try:
            diff, used = _propose(
                client,
                pack,
                config.settings,
                feedback=feedback,
                max_tokens=min(2048, remaining),
            )
            site_tokens += used
            report.tokens_used += used
            last_diff = diff
            validate_diff_scope(diff, pack)
            patched = apply_unified_diff(pack.full_source, diff)
            ensure_parses(patched)
            _verify_site_cleared(config, site, patched)
            tests_ok, test_err = _run_tests(config, site, patched)
            if not tests_ok:
                feedback = test_err or "tests failed"
                last_error = feedback
                continue
            why = ""
            why_budget = config.settings.max_tokens_per_site - site_tokens
            if why_budget > 0:
                why, why_tokens = _summarize(
                    client, pack, diff, config.settings, max_tokens=min(128, why_budget)
                )
                site_tokens += why_tokens
                report.tokens_used += why_tokens
            status = (
                SiteStatus.FIXED_VERIFIED
                if config.test_cmd
                else SiteStatus.FIXED_UNVERIFIED
            )
            if config.write:
                _write_file(config.root, site.path, patched)
            return SiteResult(
                site=site,
                status=status,
                why=why,
                diff=diff,
                attempts=attempts,
                context=pack,
            )
        except PatchError as exc:
            last_error = str(exc)
            feedback = last_error
        except Exception as exc:  # noqa: BLE001 — surface to retry / needs-human
            last_error = str(exc)
            feedback = last_error

    return SiteResult(
        site=site,
        status=SiteStatus.NEEDS_HUMAN,
        why="",
        diff=last_diff,
        attempts=attempts,
        error=last_error or "max attempts exhausted",
        context=pack,
    )


def _propose(
    client: ModelClient,
    pack: ContextPack,
    settings: TokenFactorySettings,
    *,
    feedback: str,
    max_tokens: int,
) -> tuple[str, int]:
    messages = [
        ChatMessage(role=role, content=content)
        for role, content in propose_messages(pack, feedback=feedback)
    ]
    result = client.chat(
        model=settings.model,
        messages=messages,
        max_tokens=max_tokens,
    )
    return extract_unified_diff(result.content), result.total_tokens


def _summarize(
    client: ModelClient,
    pack: ContextPack,
    diff: str,
    settings: TokenFactorySettings,
    *,
    max_tokens: int,
) -> tuple[str, int]:
    messages = [
        ChatMessage(role=role, content=content)
        for role, content in summarize_messages(pack, diff)
    ]
    result = client.chat(
        model=settings.model_small,
        messages=messages,
        max_tokens=max_tokens,
    )
    text = result.content.strip()
    why = text.splitlines()[0] if text else ""
    return why, result.total_tokens


def _verify_site_cleared(config: ApplyConfig, site: ImpactSite, patched: str) -> None:
    if config.site_cleared is None:
        return
    if not config.site_cleared(config.root, site, patched):
        raise PatchError("impact site still flagged after patch")


def _run_tests(
    config: ApplyConfig, site: ImpactSite, patched: str
) -> tuple[bool, str]:
    if not config.test_cmd:
        return True, ""
    with tempfile.TemporaryDirectory(prefix="conduit-apply-") as tmp:
        scratch = Path(tmp) / "tree"
        shutil.copytree(
            config.root,
            scratch,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                ".pytest_cache",
                "node_modules",
            ),
        )
        target = scratch / site.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patched, encoding="utf-8")
        proc = subprocess.run(
            config.test_cmd,
            shell=True,
            cwd=scratch,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True, ""
        err = (proc.stdout or "") + (proc.stderr or "")
        return False, err.strip() or f"test command exited {proc.returncode}"


def _write_file(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
