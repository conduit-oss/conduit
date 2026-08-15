"""SDKReleaseWorker: major/prerelease SDK bumps vs client's installed version."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from conduit.detect.client_state import PackageClientState
from conduit.detect.modules.openai.models_legacy import ChangeType, RawSignal, Severity
from conduit.detect.modules.openai.workers.base import Worker, fixtures_dir, resolve_profile
from conduit.detect.version_steps import (
    list_release_versions,
    next_version_step,
    parse_release_version,
    version_step_reason,
)

TAG_RE = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)$")

# Info-point registry: which repos to watch (not deprecation content).
DEFAULT_REPOS: dict[str, dict[str, Any]] = {
    "openai/openai-python": {
        "package": "openai",
        "ecosystems": ["pip", "pyproject"],
    },
    "openai/openai-node": {
        "package": "openai",
        "ecosystems": ["npm"],
    },
}


def _parse_version(tag: str) -> Version | None:
    return parse_release_version(tag)


def _is_prerelease(tag: str, version: Version) -> bool:
    return bool(version.is_prerelease) or "-rc" in tag.lower() or ".rc" in tag.lower()


def _github_release_tags(repo: str, *, per_page: int = 30, max_pages: int = 3) -> list[str]:
    """List recent release tags (newest first from GitHub)."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    tags: list[str] = []
    try:
        with httpx.Client(timeout=30.0, headers=headers) as client:
            for page in range(1, max_pages + 1):
                url = (
                    f"https://api.github.com/repos/{repo}/releases"
                    f"?per_page={per_page}&page={page}"
                )
                resp = client.get(url)
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, list) or not payload:
                    break
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    if item.get("draft"):
                        continue
                    name = item.get("tag_name")
                    if name:
                        tags.append(str(name))
                if len(payload) < per_page:
                    break
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        return tags
    return tags


def _github_latest_tag(repo: str) -> str | None:
    """Fallback single-tag fetch when list endpoint is empty/unavailable."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        return resp.json().get("tag_name")
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        return None


def _load_repo_registry(profile=None) -> dict[str, dict[str, Any]]:
    """Repos of SDK repos to poll (info points). Fixture file, profile, or defaults."""
    prof = resolve_profile(profile)
    fixture = fixtures_dir(prof.fixtures_name) / "sdk_releases" / "tags.json"
    if fixture.is_file():
        try:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            repos = payload.get("repos") or {}
            if isinstance(repos, dict) and repos:
                return repos
        except (OSError, json.JSONDecodeError):
            pass
    if prof.sdk_release_repos:
        return dict(prof.sdk_release_repos)
    return dict(DEFAULT_REPOS)


def _ecosystems_match(meta: dict[str, Any], client_ecosystems: list[str]) -> bool:
    wanted = {str(e).lower() for e in (meta.get("ecosystems") or [])}
    if not wanted:
        return True
    # Treat pyproject as pip for matching
    have = {e.lower() for e in client_ecosystems}
    if "pip" in have:
        have.add("pyproject")
    if "pyproject" in have:
        have.add("pip")
    if not have:
        # Unknown ecosystem: allow a single comparison path (prefer pip registry entries)
        return "pip" in wanted or "pyproject" in wanted
    return bool(wanted & have)


def _tags_for_repo(meta: dict[str, Any], *, demo: bool, repo: str) -> list[str]:
    if demo:
        tags = meta.get("tags")
        if isinstance(tags, list) and tags:
            return [str(t) for t in tags]
        # Fall back to previous + latest so single-step fixtures still work
        out: list[str] = []
        for key in ("previous_tag", "latest_tag"):
            if meta.get(key):
                out.append(str(meta[key]))
        return out
    tags = _github_release_tags(repo)
    if tags:
        return tags
    latest = _github_latest_tag(repo)
    return [latest] if latest else []


class SDKReleaseWorker(Worker):
    name = "SDKReleaseWorker"

    def __init__(self) -> None:
        self.last_skip_reason: str | None = None

    def run(
        self,
        *,
        demo: bool = False,
        client_state: PackageClientState | None = None,
        majors_only: bool = True,
        profile=None,
    ) -> list[RawSignal]:
        self.last_skip_reason = None
        prof = resolve_profile(profile)
        vendor = prof.name
        installed_raw = (
            (client_state.installed_version if client_state else None) or ""
        ).strip()
        if not installed_raw:
            self.last_skip_reason = "no_installed_version"
            return []

        installed_v = _parse_version(installed_raw)
        if installed_v is None:
            try:
                installed_v = Version(installed_raw.lstrip("v"))
            except InvalidVersion:
                self.last_skip_reason = "unparseable_installed_version"
                return []

        client_ecosystems = list(client_state.ecosystems) if client_state else []
        repos = _load_repo_registry(prof)
        signals: list[RawSignal] = []
        seen_packages: set[str] = set()

        for repo, meta in repos.items():
            if not _ecosystems_match(meta, client_ecosystems):
                continue
            tags = _tags_for_repo(meta, demo=demo, repo=repo)
            if not tags:
                continue

            versions = list_release_versions(tags)
            if not versions:
                continue

            latest_v = max(versions)
            chosen = next_version_step(
                installed_v, versions, majors_only=majors_only
            )
            if chosen is None:
                continue
            if chosen <= installed_v:
                continue

            package = str(meta.get("package") or repo.split("/")[-1])
            pkg_key = package.lower()
            if pkg_key in seen_packages:
                continue

            ecosystems = meta.get("ecosystems", ["pip"])
            deferred = latest_v > chosen
            pre = chosen.is_prerelease
            severity = (
                Severity.CRITICAL
                if chosen.major > installed_v.major
                else Severity.WARNING
            )
            reason = version_step_reason(
                installed_v,
                chosen,
                latest_v,
                package=package,
                majors_only=majors_only,
            )
            latest_tag = str(meta.get("latest_tag") or f"v{latest_v}")
            # Prefer a real tag string matching chosen when present
            chosen_tag = next(
                (t for t in tags if parse_release_version(t) == chosen),
                f"v{chosen}",
            )

            seen_packages.add(pkg_key)
            signals.append(
                RawSignal(
                    vendor=vendor,
                    change_type=ChangeType.SDK_MAJOR_BUMP,
                    severity=severity,
                    affected_pattern=package,
                    replacement_pattern=str(chosen.base_version),
                    source_url=f"https://github.com/{repo}/releases/tag/{chosen_tag}",
                    description=(
                        f"SDK {repo}: {reason}"
                        + (" (prerelease)" if pre else "")
                    ),
                    extra={
                        "package": package,
                        "from_version": str(installed_v.base_version),
                        "to_version": str(chosen.base_version),
                        "ecosystems": ecosystems,
                        "repo": repo,
                        "latest_tag": latest_tag,
                        "chosen_tag": chosen_tag,
                        "deferred_latest": str(latest_v.base_version) if deferred else None,
                        "majors_only": majors_only,
                        "reason": reason,
                    },
                )
            )

        return signals
