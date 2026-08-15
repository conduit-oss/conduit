"""Declarative vendor detect profile: sources, scan patterns, docs, evidence."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from conduit.detect.modules.base import DetectModule

KnownIdsFn = Callable[..., set[str]]


@dataclass
class VendorProfile:
    """Links + scan config that drive the shared detect pipeline.

    Custom parsers stay on worker subclasses; this object supplies URLs, maps,
    and optional hooks (e.g. ``known_ids``).
    """

    name: str
    packages: list[str]
    ecosystems: list[str] = field(default_factory=lambda: ["pypi"])

    model_id_pattern: str | None = None
    api_pattern: str | None = None
    known_ids: KnownIdsFn | None = None

    deprecations_url: str | None = None
    changelog_url: str | None = None
    openapi_repo: str | None = None
    openapi_source_url: str | None = None
    sdk_release_repos: dict[str, dict[str, Any]] = field(default_factory=dict)
    live_catalog_url: str | None = None
    live_catalog_auth_env: str | None = None

    models_catalog_url: str | None = None
    model_doc_url_template: str | None = None
    path_to_callees: dict[str, list[str]] = field(default_factory=dict)
    # (regex, "/v1/...") — compiled at use
    api_pattern_to_path: list[tuple[str, str]] = field(default_factory=list)

    evidence_seeds: list[str] = field(default_factory=list)
    evidence_hosts: list[str] = field(default_factory=list)
    evidence_query_templates: list[str] = field(default_factory=list)

    fixtures_name: str | None = None
    parser_kind: str = "generic"  # "openai_html" enables OpenAI HTML/RSS scrapers
    demo_packet_fallback: bool = False

    def pattern_pack(self) -> dict[str, re.Pattern[str]]:
        pack: dict[str, re.Pattern[str]] = {}
        if self.model_id_pattern:
            pack["model_id"] = re.compile(self.model_id_pattern, re.IGNORECASE)
        if self.api_pattern:
            pack["api_pattern"] = re.compile(self.api_pattern, re.IGNORECASE)
        return pack

    def model_id_re(self) -> re.Pattern[str] | None:
        return self.pattern_pack().get("model_id")

    def format_evidence_queries(self, *, from_version: str, to_version: str) -> list[str]:
        pkg = (self.packages[0] if self.packages else self.name)
        ctx = {
            "name": self.name,
            "package": pkg,
            "from_version": from_version,
            "to_version": to_version,
        }
        out: list[str] = []
        for tmpl in self.evidence_query_templates:
            try:
                out.append(tmpl.format(**ctx))
            except (KeyError, ValueError):
                out.append(tmpl)
        return out

    def model_doc_url(self, model_id: str) -> str | None:
        tmpl = self.model_doc_url_template
        if not tmpl:
            return None
        return tmpl.format(model_id=model_id.strip())

    def openapi_git_url(self) -> str | None:
        raw = (self.openapi_repo or self.openapi_source_url or "").strip()
        if not raw:
            return None
        if raw.endswith(".git") or "/blob/" in raw:
            return raw
        if raw.startswith("https://github.com/") and not raw.endswith(".git"):
            return raw.rstrip("/") + ".git"
        return raw

    def matches_package(self, package: str) -> bool:
        want = package.lower()
        if self.name.lower() == want:
            return True
        return any(p.lower() == want for p in self.packages)


def profile_for_package(package: str) -> VendorProfile | None:
    """Resolve a loaded detect module's profile for ``package`` (lazy import)."""
    from conduit.detect.modules.discovery import load_modules

    want = package.lower()
    for mod in load_modules():
        prof = _module_profile(mod)
        if prof is None:
            continue
        if prof.matches_package(want):
            return prof
        pkgs = {x.lower() for x in (mod.packages or [])}
        if want in pkgs or mod.name.lower() == want:
            return prof
    return None


def _module_profile(mod: DetectModule) -> VendorProfile | None:
    prof = getattr(mod, "profile", None)
    if isinstance(prof, VendorProfile):
        return prof
    getter = getattr(mod, "vendor_profile", None)
    if callable(getter):
        got = getter()
        if isinstance(got, VendorProfile):
            return got
    return None


def collect_known_ids(package: str, *, demo: bool = False) -> set[str]:
    """Call the package profile's known-ids hook; empty if none/unavailable."""
    prof = profile_for_package(package)
    if prof is None or prof.known_ids is None:
        return set()
    try:
        return set(prof.known_ids(demo=demo))
    except Exception:  # noqa: BLE001 — fail soft
        return set()
