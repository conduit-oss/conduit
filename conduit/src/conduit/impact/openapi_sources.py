"""Where versioned OpenAPI / Swagger / discovery documents live.

This is a location catalog and a generic discovery rule — not a list of
endpoint changes. Change data always comes from diffing two resolved specs.

Generic rule (SDK package):
  PyPI ``project_urls`` → source git repo → at each release tag, look for a
  committed OpenAPI/Swagger/discovery document (common paths, generator
  inputs, ``.stats.yml`` ``openapi_spec_url``, Speakeasy workflow inputs).

Also supported: public registries with git history (APIs.guru openapi-directory,
Azure azure-rest-api-specs, Google discovery-artifact-manager).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import yaml

#: Cache directory for downloaded OpenAPI documents.
_CACHE = Path("/tmp/conduit-openapi")

#: Filenames / relative paths commonly committed next to generated SDKs.
COMMITTED_SPEC_PATHS: tuple[str, ...] = (
    "openapi.yaml",
    "openapi.yml",
    "openapi.json",
    "swagger.yaml",
    "swagger.yml",
    "swagger.json",
    "openapi/openapi.yaml",
    "openapi/openapi.yml",
    "openapi/openapi.json",
    "openapi/spec3.json",
    "openapi/spec3.sdk.json",
    "spec/openapi.yaml",
    "spec/openapi.yml",
    "spec/openapi.json",
    "spec/v2.yaml",
    "spec/v2.yml",
    "spec/swagger.yaml",
    "spec/swagger.json",
    "specs/openapi.yaml",
    "specs/openapi.yml",
    "specs/openapi.json",
    "api/openapi.yaml",
    "api/openapi.yml",
    "api/openapi.json",
    "swagger/swagger.json",
    "swagger/swagger.yaml",
)

#: Directories to probe for committed ``*.yaml`` / ``*.yml`` / ``*.json`` specs.
COMMITTED_SPEC_DIRS: tuple[str, ...] = (
    "openapi",
    "spec",
    "specs",
    "api",
    ".speakeasy",
    "swagger",
)

#: Stainless / Castiron stats files that may expose ``openapi_spec_url``.
STATS_FILES: tuple[str, ...] = (".stats.yml", ".castiron.stats.yml")

_GITHUB_REPO = re.compile(
    r"https?://(?:www\.)?github\.com/([^/\s\"'#]+)/([^/\s\"'#]+?)(?:\.git)?/?$",
    re.I,
)


@dataclass(frozen=True)
class SpecSource:
    """How to resolve a from/to OpenAPI document for one outside service."""

    service_id: str
    kind: str
    #: ``owner/repo`` on GitHub.
    repo: str = ""
    #: Path inside the git repo (git_repo / google_discovery kinds).
    path: str = ""
    #: Default branch tip when a ref cannot be resolved.
    default_branch: str = "main"
    #: ``tag`` (stripe-style ``vNNNN``) or ``commit_by_date``.
    ref_style: str = ""
    #: Stats file for Stainless-generated SDKs (``openapi_spec_url`` when present).
    stats_file: str = ".stats.yml"
    #: APIs.guru provider directory under ``APIs/`` (e.g. ``github.com``).
    apis_guru_provider: str = ""
    #: Azure ``specification/{name}/…`` root segment.
    azure_service: str = ""
    #: Google discovery filename stem (e.g. ``drive.v3``).
    google_discovery: str = ""
    #: Human-readable note about what we verified in the wild.
    note: str = ""


#: Host / service → where a versioned OpenAPI history can be fetched.
#: Verified against the live repos (see docs/backtest.md).
SPEC_SOURCES: dict[str, SpecSource] = {}

def spec_source_for(service_id: str) -> SpecSource | None:
    return SPEC_SOURCES.get((service_id or "").lower())


def resolve_spec_pair(
    service_id: str,
    *,
    from_ref: str,
    to_ref: str,
    allow_network: bool = True,
    package: str = "",
) -> tuple[Path | None, Path | None, str]:
    """Download the from/to OpenAPI documents for ``service_id`` or ``package``.

    ``from_ref`` / ``to_ref`` are API versions, SDK versions, ISO dates
    (``YYYY-MM-DD``), or git tags/SHAs depending on the source kind.

    Resolution order:
    1. Catalogued ``SPEC_SOURCES`` for ``service_id``
    2. Generic SDK package discovery (PyPI → source repo → tag specs)
    3. Registry aliases when ``service_id`` names a known registry entry

    Returns ``(old_path, new_path, note)``. Paths are set when both documents
    were fetched. ``note`` explains a miss when either side is missing.
    """
    if not allow_network:
        source = spec_source_for(service_id)
        if source is not None:
            return None, None, (
                f"not compared: {service_id} has a versioned OpenAPI source "
                f"({_source_label(source)}) but network is disabled"
            )
        pkg = (package or service_id or "").strip()
        if pkg:
            return None, None, (
                f"not compared: generic SDK/OpenAPI discovery for {pkg} "
                f"needs network"
            )
        return None, None, (
            f"not compared: no versioned OpenAPI source is catalogued for {service_id}"
        )
    if not from_ref or not to_ref:
        return None, None, (
            f"not compared: {service_id or package} REST calls need from and to "
            f"versions to pick OpenAPI/discovery documents"
        )

    source = spec_source_for(service_id)
    if source is not None:
        old, new, note = _resolve_catalogued(
            source, from_ref=from_ref, to_ref=to_ref
        )
        if old is not None and new is not None:
            return old, new, note
        # Registry fallbacks (e.g. github → APIs.guru) when the primary
        # location has no history for the requested refs.
        for alt_id in _REGISTRY_FALLBACKS.get((service_id or "").lower(), ()):
            alt = spec_source_for(alt_id)
            if alt is None:
                continue
            a_old, a_new, a_note = _resolve_catalogued(
                alt, from_ref=from_ref, to_ref=to_ref
            )
            if a_old is not None and a_new is not None:
                return a_old, a_new, ""
            if a_note:
                note = f"{note}; fallback {alt_id}: {a_note}" if note else a_note
        if note:
            return None, None, note

    pkg = (package or service_id or "").strip()
    if pkg:
        old, new, note = resolve_sdk_package_pair(
            pkg, from_ref=from_ref, to_ref=to_ref
        )
        if old is not None and new is not None:
            return old, new, note
        # Fall through to a clearer catalog miss when package discovery failed.
        if "no source repo" not in note and "no OpenAPI" not in note:
            return old, new, note
        catalog_miss = (
            f"not compared: no versioned OpenAPI source is catalogued for "
            f"{service_id}"
        )
        return None, None, f"{note}; {catalog_miss}" if note else catalog_miss

    return None, None, (
        f"not compared: no versioned OpenAPI source is catalogued for {service_id}"
    )


#: When the primary catalogued source cannot resolve a from/to pair, try these
#: public registries (same host/service, different git history).
_REGISTRY_FALLBACKS: dict[str, tuple[str, ...]] = {}

def resolve_sdk_package_pair(
    package: str,
    *,
    from_ref: str,
    to_ref: str,
) -> tuple[Path | None, Path | None, str]:
    """PyPI project_urls → git repo → discover specs at from/to release tags."""
    repo = pypi_source_repo(package)
    if not repo:
        return None, None, (
            f"not compared: PyPI package {package!r} has no GitHub source in "
            f"project_urls"
        )
    old_loc, old_why = discover_spec_at_ref(repo, from_ref)
    new_loc, new_why = discover_spec_at_ref(repo, to_ref)
    if not old_loc or not new_loc:
        reasons = "; ".join(item for item in (old_why, new_why) if item)
        return None, None, (
            f"not compared: no OpenAPI/Swagger/discovery spec at tags for "
            f"{package} ({repo}) {from_ref!r} → {to_ref!r} ({reasons or 'unknown'})"
        )
    old = _materialize_location(old_loc, prefix=f"{package}-from")
    new = _materialize_location(new_loc, prefix=f"{package}-to")
    if old is None or new is None:
        return None, None, (
            f"not compared: discovered spec for {package} ({repo}) could not be "
            f"downloaded"
        )
    return old, new, ""


def pypi_source_repo(package: str) -> str | None:
    """Return ``owner/repo`` from PyPI ``project_urls`` / home_page when GitHub."""
    if not package:
        return None
    meta_url = f"https://pypi.org/pypi/{package}/json"
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(meta_url)
            if response.status_code != 200:
                return None
            meta = response.json()
    except Exception:
        return None
    info = meta.get("info") if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return None
    urls: list[str] = []
    project_urls = info.get("project_urls")
    if isinstance(project_urls, dict):
        # Prefer explicit source keys, then the rest.
        preferred = (
            "Source",
            "Source Code",
            "Source code",
            "Repository",
            "repository",
            "Homepage",
            "Home",
            "Code",
            "GitHub",
        )
        for key in preferred:
            value = project_urls.get(key)
            if isinstance(value, str) and value.strip():
                urls.append(value.strip())
        for value in project_urls.values():
            if isinstance(value, str) and value.strip() and value.strip() not in urls:
                urls.append(value.strip())
    home = info.get("home_page")
    if isinstance(home, str) and home.strip():
        urls.append(home.strip())
    for url in urls:
        repo = _github_owner_repo(url)
        if repo:
            return repo
    return None


def discover_spec_at_ref(repo: str, ref: str) -> tuple[str | None, str]:
    """Locate a spec at ``repo@ref``.

    Returns ``(location, reason)``. ``location`` is ``raw:owner/repo@ref/path``,
    ``url:<https…>``, or None.
    """
    tag_candidates = _tag_candidates(ref)
    last_why = ""
    for tag in tag_candidates:
        loc, why = _discover_at_exact_ref(repo, tag)
        if loc:
            return loc, ""
        last_why = why or last_why
    return None, last_why or f"no spec at tags for {ref!r}"


def _discover_at_exact_ref(repo: str, ref: str) -> tuple[str | None, str]:
    # 1. Common committed paths.
    for path in COMMITTED_SPEC_PATHS:
        if _raw_exists(repo, ref, path) and _looks_like_spec_path(path):
            return f"raw:{repo}@{ref}/{path}", ""

    # 2. Stainless / Castiron stats → openapi_spec_url.
    for stats_file in STATS_FILES:
        url, why = _stats_openapi_url(repo, ref, stats_file)
        if url:
            return f"url:{url}", ""
        if why and "no openapi_spec_url" in why:
            # Stats present but useless — keep looking.
            pass

    # 3. Speakeasy workflow inputs (external or relative OpenAPI location).
    speakeasy_locs = _speakeasy_locations(repo, ref)
    for loc in speakeasy_locs:
        if loc.startswith("url:") or loc.startswith("raw:"):
            return loc, ""

    # 4. Probe common directories via the GitHub contents API.
    for directory in COMMITTED_SPEC_DIRS:
        found = _first_spec_in_dir(repo, ref, directory)
        if found:
            return f"raw:{repo}@{ref}/{found}", ""

    # 5. Fern marker — definitions are almost never committed in the SDK repo.
    if _raw_exists(repo, ref, ".fernignore") or _raw_exists(
        repo, ref, ".fern/metadata.json"
    ):
        return None, (
            f"{ref}: Fern-generated SDK (.fern) without a committed OpenAPI/"
            f"Swagger document"
        )

    # 6. OpenAPI Generator marker without a discovered spec path.
    if _raw_exists(repo, ref, ".openapi-generator-ignore"):
        return None, (
            f"{ref}: OpenAPI Generator SDK (.openapi-generator-ignore) but no "
            f"spec/ or openapi file at common paths"
        )

    return None, f"{ref}: no OpenAPI/Swagger/discovery file at common paths"


def _resolve_catalogued(
    source: SpecSource, *, from_ref: str, to_ref: str
) -> tuple[Path | None, Path | None, str]:
    if source.kind == "git_repo":
        return _resolve_git_pair(source, from_ref=from_ref, to_ref=to_ref)
    if source.kind == "stainless_sdk":
        return _resolve_stainless_pair(source, from_ref=from_ref, to_ref=to_ref)
    if source.kind == "apis_guru":
        return _resolve_apis_guru_pair(source, from_ref=from_ref, to_ref=to_ref)
    if source.kind == "azure_specs":
        return _resolve_azure_pair(source, from_ref=from_ref, to_ref=to_ref)
    if source.kind == "google_discovery":
        return _resolve_git_pair(source, from_ref=from_ref, to_ref=to_ref)
    # Generic SDK repo listed in the catalog (path empty → discover at tags).
    if source.kind == "sdk_repo":
        return resolve_sdk_package_pair(
            source.service_id, from_ref=from_ref, to_ref=to_ref
        ) if not source.repo else _resolve_sdk_repo_pair(
            source, from_ref=from_ref, to_ref=to_ref
        )
    return None, None, f"not compared: unknown OpenAPI source kind {source.kind!r}"


def _resolve_sdk_repo_pair(
    source: SpecSource, *, from_ref: str, to_ref: str
) -> tuple[Path | None, Path | None, str]:
    old_loc, old_why = discover_spec_at_ref(source.repo, from_ref)
    new_loc, new_why = discover_spec_at_ref(source.repo, to_ref)
    if not old_loc or not new_loc:
        reasons = "; ".join(item for item in (old_why, new_why) if item)
        return None, None, (
            f"not compared: no OpenAPI at tags for {source.repo} "
            f"{from_ref!r} → {to_ref!r} ({reasons or 'unknown'}). {source.note}"
        )
    old = _materialize_location(old_loc, prefix=f"{source.service_id}-from")
    new = _materialize_location(new_loc, prefix=f"{source.service_id}-to")
    if old is None or new is None:
        return None, None, (
            f"not compared: discovered spec for {source.repo} could not be downloaded"
        )
    return old, new, ""


def _source_label(source: SpecSource) -> str:
    if source.kind == "git_repo":
        return f"{source.repo}/{source.path}"
    if source.kind == "stainless_sdk":
        return f"{source.repo}/{source.stats_file}"
    if source.kind == "apis_guru":
        return f"{source.repo}/APIs/{source.apis_guru_provider}"
    if source.kind == "azure_specs":
        return f"{source.repo}/specification/{source.azure_service}"
    if source.kind == "google_discovery":
        return f"{source.repo}/{source.path}"
    if source.kind == "sdk_repo":
        return source.repo or source.service_id
    return source.kind


def _resolve_git_pair(
    source: SpecSource, *, from_ref: str, to_ref: str
) -> tuple[Path | None, Path | None, str]:
    old_ref, old_why = _git_ref(source, from_ref)
    new_ref, new_why = _git_ref(source, to_ref)
    if not old_ref or not new_ref:
        reasons = "; ".join(item for item in (old_why, new_why) if item)
        return None, None, (
            f"not compared: could not resolve OpenAPI refs for {source.service_id} "
            f"in {source.repo} ({reasons or 'unknown'})"
        )
    old = _download_raw(source.repo, old_ref, source.path)
    new = _download_raw(source.repo, new_ref, source.path)
    if old is None or new is None:
        return None, None, (
            f"not compared: OpenAPI file missing at "
            f"{source.repo}@{{{old_ref} → {new_ref}}}/{source.path}"
        )
    return old, new, ""


def _git_ref(source: SpecSource, token: str) -> tuple[str, str]:
    """Map a user/version/date token to a git ref that has the spec file."""
    text = (token or "").strip()
    if not text:
        return "", "empty ref"
    if source.ref_style == "tag":
        return _tag_ref(source, text)
    if source.ref_style == "commit_by_date":
        return _commit_by_date(source, text)
    return text, ""


def _tag_ref(source: SpecSource, token: str) -> tuple[str, str]:
    """Accept an exact tag, ``v``+number, a date, or a PyPI release date."""
    if _raw_exists(source.repo, token, source.path):
        return token, ""
    if token.isdigit() and _raw_exists(source.repo, f"v{token}", source.path):
        return f"v{token}", ""
    if token.lower().startswith("v") and token[1:].isdigit():
        if _raw_exists(source.repo, token, source.path):
            return token, ""
    date = _parse_date(token) or _pypi_release_date(source.service_id, token)
    if date is not None:
        sha = _commit_before(source.repo, source.path, date)
        if sha:
            return sha, ""
        return "", f"no commit at or before {token}"
    return "", f"no OpenAPI at tag/ref {token!r}"


def _commit_by_date(source: SpecSource, token: str) -> tuple[str, str]:
    if len(token) >= 7 and all(ch in "0123456789abcdef" for ch in token.lower()):
        if _raw_exists(source.repo, token, source.path):
            return token, ""
    date = _parse_date(token) or _pypi_release_date(source.service_id, token)
    if date is None:
        if _raw_exists(source.repo, token, source.path):
            return token, ""
        return "", f"not a date or commit ref: {token!r}"
    path = source.path
    if not path and source.kind == "apis_guru":
        path = _apis_guru_path(source, token) or ""
    if not path and source.kind == "azure_specs":
        path = _azure_swagger_path(source, token) or ""
    if not path:
        return "", f"no path for {source.service_id}"
    sha = _commit_before(source.repo, path, date)
    if not sha:
        api_err = f" ({_GITHUB_API_LAST_ERROR})" if _GITHUB_API_LAST_ERROR else ""
        return "", f"no commit for {path} at or before {token}{api_err}"
    return sha, ""


def _pypi_release_date(package: str, version: str) -> datetime | None:
    """Upload time of a PyPI release, used when the token is a client pin."""
    if not package or not version or not version[0].isdigit():
        return None
    meta_url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(meta_url)
            if response.status_code != 200:
                return None
            meta = response.json()
    except Exception:
        return None
    urls = meta.get("urls") if isinstance(meta, dict) else None
    if not isinstance(urls, list):
        return None
    stamps = [
        _parse_date(str(item.get("upload_time_iso_8601") or item.get("upload_time") or ""))
        for item in urls
        if isinstance(item, dict)
    ]
    stamps = [item for item in stamps if item is not None]
    return min(stamps) if stamps else None


def _resolve_stainless_pair(
    source: SpecSource, *, from_ref: str, to_ref: str
) -> tuple[Path | None, Path | None, str]:
    old_url, old_why = _stainless_url(source, from_ref)
    new_url, new_why = _stainless_url(source, to_ref)
    if not old_url or not new_url:
        reasons = "; ".join(item for item in (old_why, new_why) if item)
        return None, None, (
            f"not compared: {source.repo} {source.stats_file} has no "
            f"openapi_spec_url for {from_ref!r} → {to_ref!r} ({reasons}). "
            f"{source.note}"
        )
    old = _download_url(old_url, prefix=f"{source.service_id}-from")
    new = _download_url(new_url, prefix=f"{source.service_id}-to")
    if old is None or new is None:
        return None, None, (
            f"not compared: openapi_spec_url from {source.repo} could not be downloaded"
        )
    return old, new, ""


def _stainless_url(source: SpecSource, version: str) -> tuple[str, str]:
    for candidate in _tag_candidates(version):
        url, why = _stats_openapi_url(source.repo, candidate, source.stats_file)
        if url:
            return url, ""
        if why:
            return "", why
    return "", f"no {source.stats_file} at tags for {version!r}"


def _stats_openapi_url(repo: str, ref: str, stats_file: str) -> tuple[str, str]:
    text = _fetch_text(
        f"https://raw.githubusercontent.com/{repo}/{quote(ref, safe='')}/"
        f"{stats_file}"
    )
    if text is None:
        return "", ""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return "", f"{ref}: stats file is not YAML"
    if not isinstance(data, dict):
        return "", f"{ref}: stats file is empty"
    url = str(data.get("openapi_spec_url") or "").strip()
    if url:
        return url, ""
    return "", (
        f"{ref}: {stats_file} has no openapi_spec_url "
        f"(keys: {', '.join(sorted(str(k) for k in data.keys()))})"
    )


def _speakeasy_locations(repo: str, ref: str) -> list[str]:
    """Read ``.speakeasy/workflow.yaml`` inputs and resolve to raw:/url: locations."""
    text = _fetch_text(
        f"https://raw.githubusercontent.com/{repo}/{quote(ref, safe='')}/"
        f".speakeasy/workflow.yaml"
    )
    if text is None:
        # Some repos keep gen.yaml / workflow at repo root.
        text = _fetch_text(
            f"https://raw.githubusercontent.com/{repo}/{quote(ref, safe='')}/"
            f"workflow.yaml"
        )
    if text is None:
        return []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    found: list[str] = []
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return []
    for source in sources.values():
        if not isinstance(source, dict):
            continue
        inputs = source.get("inputs")
        if not isinstance(inputs, list):
            continue
        for item in inputs:
            if not isinstance(item, dict):
                continue
            location = str(item.get("location") or "").strip()
            if not location:
                continue
            found.extend(_normalize_speakeasy_location(repo, ref, location))
    return found


def _normalize_speakeasy_location(repo: str, ref: str, location: str) -> list[str]:
    if location.startswith(("http://", "https://")):
        # Prefer GitHub raw URLs that pin a ref; otherwise fetch the URL as-is
        # (tip documents are not a from/to pair by themselves — the workflow
        # file at each SDK tag is what versions the pointer).
        gh = _github_raw_from_url(location)
        if gh:
            return [f"raw:{gh}"]
        return [f"url:{location}"]
    rel = location.lstrip("./")
    if _raw_exists(repo, ref, rel):
        return [f"raw:{repo}@{ref}/{rel}"]
    return []


def _github_raw_from_url(url: str) -> str | None:
    """Map a github.com / raw.githubusercontent.com URL to ``owner/repo@ref/path``."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    parts = [p for p in (parsed.path or "").split("/") if p]
    if host == "raw.githubusercontent.com" and len(parts) >= 4:
        owner, name, ref = parts[0], parts[1], parts[2]
        path = "/".join(parts[3:])
        return f"{owner}/{name}@{ref}/{path}"
    if host in {"github.com", "www.github.com"} and len(parts) >= 5:
        # /owner/repo/blob|raw/ref/path  or  /owner/repo/refs/heads/ref/path
        owner, name = parts[0], parts[1]
        if parts[2] in {"blob", "raw"} and len(parts) >= 5:
            ref = parts[3]
            path = "/".join(parts[4:])
            return f"{owner}/{name}@{ref}/{path}"
        if parts[2] == "refs" and len(parts) >= 6:
            ref = parts[4]
            path = "/".join(parts[5:])
            return f"{owner}/{name}@{ref}/{path}"
    return None


def _resolve_apis_guru_pair(
    source: SpecSource, *, from_ref: str, to_ref: str
) -> tuple[Path | None, Path | None, str]:
    old_path = _apis_guru_path(source, from_ref)
    new_path = _apis_guru_path(source, to_ref)
    if not old_path or not new_path:
        return None, None, (
            f"not compared: no APIs.guru OpenAPI under APIs/"
            f"{source.apis_guru_provider} for {from_ref!r} → {to_ref!r}. "
            f"{source.note}"
        )
    # Prefer version-directory snapshots when refs name those dirs.
    if old_path != new_path:
        old_file = _download_raw(source.repo, source.default_branch, old_path)
        new_file = _download_raw(source.repo, source.default_branch, new_path)
        if old_file is not None and new_file is not None:
            return old_file, new_file, ""
    # Same path → walk git history by date (or by commit SHA).
    path = old_path
    # Also try history on the provider directory when the leaf path is new.
    git_source = SpecSource(
        service_id=source.service_id,
        kind="git_repo",
        repo=source.repo,
        path=path,
        ref_style="commit_by_date",
    )
    old_sha, old_why = _commit_by_date(git_source, from_ref)
    new_sha, new_why = _commit_by_date(git_source, to_ref)
    if not old_sha or not new_sha:
        # Provider-level history (weekly updates often touch the tree, not one file).
        parent = str(Path(path).parent).replace("\\", "/")
        parent_source = SpecSource(
            service_id=source.service_id,
            kind="git_repo",
            repo=source.repo,
            path=parent,
            ref_style="commit_by_date",
        )
        if not old_sha:
            old_sha, old_why = _commit_by_date(parent_source, from_ref)
        if not new_sha:
            new_sha, new_why = _commit_by_date(parent_source, to_ref)
    if not old_sha or not new_sha:
        reasons = "; ".join(item for item in (old_why, new_why) if item)
        return None, None, (
            f"not compared: APIs.guru {source.apis_guru_provider} has no "
            f"resolvable history for {from_ref!r} → {to_ref!r} ({reasons}). "
            f"{source.note}"
        )
    old = _download_raw(source.repo, old_sha, path)
    new = _download_raw(source.repo, new_sha, path)
    if old is None or new is None:
        # At older commits the leaf path may differ; try openapi.yaml in parent.
        parent = str(Path(path).parent).replace("\\", "/")
        for candidate in (
            path,
            f"{parent}/openapi.yaml",
            f"{parent}/openapi.json",
        ):
            if old is None:
                old = _download_raw(source.repo, old_sha, candidate)
            if new is None:
                new = _download_raw(source.repo, new_sha, candidate)
    if old is None or new is None:
        return None, None, (
            f"not compared: APIs.guru file missing at {path} for "
            f"{old_sha[:8]} → {new_sha[:8]}"
        )
    return old, new, ""


def _apis_guru_path(source: SpecSource, token: str) -> str | None:
    provider = source.apis_guru_provider
    if not provider:
        return None
    # Direct version directory: APIs/{provider}/{token}/openapi.yaml
    for name in (
        f"APIs/{provider}/{token}/openapi.yaml",
        f"APIs/{provider}/{token}/openapi.json",
        f"APIs/{provider}/{token}/swagger.yaml",
        f"APIs/{provider}/{token}/swagger.json",
    ):
        if _raw_exists(source.repo, source.default_branch, name):
            return name
    # Tip listing: pick openapi under provider (possibly nested one level).
    entries = _github_contents(source.repo, f"APIs/{provider}", ref=source.default_branch)
    files = [e for e in entries if e.get("type") == "file"]
    dirs = [e for e in entries if e.get("type") == "dir"]
    for item in files:
        path = str(item.get("path") or "")
        if path.rsplit("/", 1)[-1] in {
            "openapi.yaml",
            "openapi.json",
            "swagger.yaml",
            "swagger.json",
        }:
            return path
    names = [str(d.get("name") or "") for d in dirs if d.get("name")]
    if not names:
        return None
    # Prefer an exact version-dir match; else dirs that already contain openapi.yaml;
    # else a short canonical id (api.github.com before api.github.com.DATE).
    def _openapi_under(base: str) -> str | None:
        for filename in ("openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json"):
            path = f"{base}/{filename}"
            if _raw_exists(source.repo, source.default_branch, path):
                return path
        nested = _github_contents(source.repo, base, ref=source.default_branch)
        for item in nested:
            path = str(item.get("path") or "")
            if path.endswith(
                ("openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json")
            ):
                return path
            if item.get("type") != "dir":
                continue
            for filename in ("openapi.yaml", "openapi.json"):
                candidate = f"{path}/{filename}"
                if _raw_exists(source.repo, source.default_branch, candidate):
                    return candidate
        return None

    if token in names:
        order = [token]
    else:
        scored: list[tuple[int, int, int, str]] = []
        for name in names:
            base = f"APIs/{provider}/{name}"
            has_openapi = _openapi_under(base) is not None
            # Prefer canonical api.* hosts over dated snapshots and tiny stubs.
            score = (
                0 if has_openapi else 1,
                0 if name.startswith("api.") else 1,
                0 if name.count(".") == 2 else 1,
                len(name),
            )
            scored.append((*score, name))
        scored.sort()
        order = [item[-1] for item in scored]
    for chosen in order:
        found = _openapi_under(f"APIs/{provider}/{chosen}")
        if found:
            return found
    return None


def _resolve_azure_pair(
    source: SpecSource, *, from_ref: str, to_ref: str
) -> tuple[Path | None, Path | None, str]:
    old_path = _azure_swagger_path(source, from_ref)
    new_path = _azure_swagger_path(source, to_ref)
    if not old_path or not new_path:
        return None, None, (
            f"not compared: no Azure swagger under specification/"
            f"{source.azure_service} for {from_ref!r} → {to_ref!r}. {source.note}"
        )
    if old_path != new_path:
        old = _download_raw(source.repo, source.default_branch, old_path)
        new = _download_raw(source.repo, source.default_branch, new_path)
        if old is not None and new is not None:
            return old, new, ""
    # Same path or tip miss → commit-by-date on the resolved path.
    path = new_path or old_path
    git_source = SpecSource(
        service_id=source.service_id,
        kind="git_repo",
        repo=source.repo,
        path=path,
        default_branch=source.default_branch,
        ref_style="commit_by_date",
    )
    return _resolve_git_pair(git_source, from_ref=from_ref, to_ref=to_ref)


def _azure_swagger_path(source: SpecSource, token: str) -> str | None:
    service = source.azure_service
    if not service:
        return None
    root = f"specification/{service}"
    # Fast path: token is an API version directory name somewhere under root.
    # Dates like 2019-06-01 may coincide with Azure API versions; if nothing
    # matches, fall through to a default swagger and resolve by commit date.
    if token and token[0].isdigit() and "T" not in token:
        matches = _azure_find_version_files(source.repo, root, token)
        if matches:
            return matches[0]
    # Otherwise pick a stable swagger near the tip and let commit_by_date version it.
    return _azure_default_swagger(source.repo, root)


def _azure_find_version_files(repo: str, root: str, version: str) -> list[str]:
    """Find ``…/stable|preview/{version}/openapi.json`` (or ``*.json``) via contents walk."""
    found: list[str] = []
    for channel in ("stable", "preview"):
        for base in _azure_channel_dirs(repo, root, channel):
            path = f"{base}/{version}"
            entries = _github_contents(repo, path, ref="main")
            for item in entries:
                if item.get("type") != "file":
                    continue
                name = str(item.get("name") or "").lower()
                full = str(item.get("path") or "")
                if name in {"openapi.json", "swagger.json"} or (
                    name.endswith(".json") and "example" not in name
                ):
                    found.append(full)
    found.sort(
        key=lambda p: (
            0 if p.rsplit("/", 1)[-1] in {"openapi.json", "swagger.json"} else 1,
            len(p),
            p,
        )
    )
    return found


def _azure_default_swagger(repo: str, root: str) -> str | None:
    """Pick the newest stable ``openapi.json`` under ``specification/{service}``."""
    for channel in ("stable", "preview"):
        for base in _azure_channel_dirs(repo, root, channel):
            versions = [
                str(item.get("name") or "")
                for item in _github_contents(repo, base, ref="main")
                if item.get("type") == "dir"
            ]
            versions = [v for v in versions if v]
            if not versions:
                continue
            versions.sort()
            for version in reversed(versions):
                entries = _github_contents(repo, f"{base}/{version}", ref="main")
                files = [
                    str(item.get("path") or "")
                    for item in entries
                    if item.get("type") == "file"
                    and str(item.get("name") or "").lower()
                    in {"openapi.json", "swagger.json"}
                ]
                if files:
                    return files[0]
                # Older layout: ``storage.json`` / ``web.json`` etc.
                other = [
                    str(item.get("path") or "")
                    for item in entries
                    if item.get("type") == "file"
                    and str(item.get("name") or "").lower().endswith(".json")
                    and "example" not in str(item.get("name") or "").lower()
                ]
                if other:
                    return sorted(other)[0]
    return None


def _azure_channel_dirs(repo: str, root: str, channel: str) -> list[str]:
    """Return directories that contain a ``stable`` / ``preview`` child under ``root``."""
    found: list[str] = []

    def walk(path: str, depth: int) -> None:
        if depth > 6 or len(found) >= 8:
            return
        entries = _github_contents(repo, path, ref="main")
        names = {str(item.get("name") or ""): item for item in entries}
        if channel in names and names[channel].get("type") == "dir":
            found.append(f"{path}/{channel}")
            return
        for item in entries:
            if item.get("type") != "dir":
                continue
            name = str(item.get("name") or "")
            if name in {"examples", "cspell", ".github"} or name.endswith(".md"):
                continue
            # Prefer resource-manager / data-plane product folders.
            if depth == 0 and name not in {"resource-manager", "data-plane"}:
                continue
            walk(str(item.get("path") or f"{path}/{name}"), depth + 1)

    walk(root, 0)
    return found


def _commit_before(repo: str, path: str, when: datetime) -> str:
    until = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"https://api.github.com/repos/{repo}/commits"
        f"?path={quote(path)}&until={quote(until)}&per_page=1"
    )
    payload, _link = _github_json(url)
    if not isinstance(payload, list) or not payload:
        return ""
    first = payload[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("sha") or "")


# Last GitHub API failure (status + short reason). Cleared on success.
_GITHUB_API_LAST_ERROR: str = ""


def github_api_available() -> bool:
    """True when the GitHub API accepts authenticated (or light unauth) traffic.

    Live OpenAPI commit-by-date tests call this to skip when CI has no token
    and the shared runner IP is rate-limited.
    """
    global _GITHUB_API_LAST_ERROR
    payload, _link = _github_json("https://api.github.com/rate_limit")
    if isinstance(payload, dict):
        core = payload.get("resources", {})
        if isinstance(core, dict):
            core = core.get("core", {})
        if isinstance(core, dict):
            remaining = core.get("remaining")
            if isinstance(remaining, int) and remaining <= 0:
                _GITHUB_API_LAST_ERROR = "rate limit remaining=0"
                return False
        return True
    return not bool(_GITHUB_API_LAST_ERROR)


def _parse_date(text: str) -> datetime | None:
    token = (text or "").strip()
    if not token:
        return None
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    for candidate in (token, token + "T00:00:00+00:00" if len(token) == 10 else ""):
        if not candidate:
            continue
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _tag_candidates(version: str) -> list[str]:
    text = (version or "").strip()
    if not text:
        return []
    out: list[str] = []
    for candidate in (text, f"v{text.lstrip('v')}", text.lstrip("v")):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _github_owner_repo(url: str) -> str | None:
    text = (url or "").strip()
    match = _GITHUB_REPO.match(text.rstrip("/"))
    if match:
        owner, name = match.group(1), match.group(2)
        if owner and name and owner.lower() not in {"topics", "settings"}:
            return f"{owner}/{name}"
    # Also accept https://github.com/owner/repo/tree/...
    parsed = urlparse(text)
    if (parsed.netloc or "").lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [p for p in (parsed.path or "").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return None


def _looks_like_spec_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name in {"gen.yaml", "workflow.yaml", "workflow.lock", "gen.lock", "metadata.json"}:
        return False
    return name.endswith((".yaml", ".yml", ".json"))


def _first_spec_in_dir(repo: str, ref: str, directory: str) -> str | None:
    entries = _github_contents(repo, directory, ref=ref)
    ranked: list[str] = []
    for item in entries:
        if item.get("type") != "file":
            continue
        path = str(item.get("path") or "")
        name = path.rsplit("/", 1)[-1].lower()
        if not _looks_like_spec_path(path):
            continue
        if name.startswith("."):
            continue
        # Prefer openapi/swagger names over overlays / examples.
        if name.startswith(("openapi", "swagger", "spec")) or name in {
            "v2.yaml",
            "v2.yml",
            "v3.yaml",
            "v3.yml",
        }:
            ranked.insert(0, path)
        elif "overlay" in name or "example" in name or "fix" in name:
            continue
        else:
            ranked.append(path)
    return ranked[0] if ranked else None


def _github_contents(repo: str, path: str, *, ref: str) -> list[dict[str, Any]]:
    url = (
        f"https://api.github.com/repos/{repo}/contents/"
        f"{quote(path, safe='/')}?ref={quote(ref, safe='')}"
    )
    payload, _ = _github_json(url)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _materialize_location(location: str, *, prefix: str) -> Path | None:
    if location.startswith("url:"):
        return _download_url(location[4:], prefix=prefix)
    if location.startswith("raw:"):
        body = location[4:]
        # owner/repo@ref/path
        if "@" not in body:
            return None
        repo_part, _, rest = body.partition("@")
        if "/" not in rest:
            return None
        ref, path = _split_ref_path(rest)
        if not ref or not path:
            return None
        return _download_raw(repo_part, ref, path)
    return None


def _split_ref_path(rest: str) -> tuple[str, str]:
    """Split ``ref/path`` where ref may be ``refs/heads/main`` or a plain tag/sha."""
    text = (rest or "").strip("/")
    if text.startswith("refs/heads/") or text.startswith("refs/tags/"):
        parts = text.split("/")
        # refs / heads|tags / name / path…
        if len(parts) >= 4:
            return "/".join(parts[:3]), "/".join(parts[3:])
        return "", ""
    parts = text.split("/")
    if len(parts) < 2:
        return "", ""
    return parts[0], "/".join(parts[1:])


def _raw_exists(repo: str, ref: str, path: str) -> bool:
    url = (
        f"https://raw.githubusercontent.com/{repo}/"
        f"{quote(ref, safe='')}/{path.lstrip('/')}"
    )
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.head(url)
            if response.status_code == 405:
                response = client.get(url, headers={"Range": "bytes=0-0"})
            return response.status_code == 200
    except Exception:
        return False


def _download_raw(repo: str, ref: str, path: str) -> Path | None:
    url = (
        f"https://raw.githubusercontent.com/{repo}/"
        f"{quote(ref, safe='')}/{path.lstrip('/')}"
    )
    safe_ref = ref.replace("/", "_")[:40]
    return _download_url(url, prefix=f"{repo.replace('/', '-')}-{safe_ref}")


def _download_url(url: str, *, prefix: str) -> Path | None:
    _CACHE.mkdir(parents=True, exist_ok=True)
    suffix = ".yaml" if url.rstrip("/").endswith((".yaml", ".yml")) else ".json"
    digest = abs(hash(url)) % (10**12)
    dest = _CACHE / f"{prefix}-{digest}{suffix}"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            dest.write_bytes(response.content)
    except Exception:
        return None
    return dest if dest.is_file() and dest.stat().st_size > 0 else None


def _fetch_text(url: str) -> str | None:
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code != 200:
                return None
            return response.text
    except Exception:
        return None


def _github_json(url: str) -> tuple[Any, str]:
    global _GITHUB_API_LAST_ERROR
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "conduit-openapi",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            if response.status_code == 200:
                _GITHUB_API_LAST_ERROR = ""
                return response.json(), response.headers.get("Link", "")
            # One short retry on secondary rate limit / transient abuse.
            if response.status_code in {403, 429}:
                import time

                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 5.0) if retry_after else 1.5
                except ValueError:
                    delay = 1.5
                time.sleep(delay)
                response = client.get(url, headers=headers)
                if response.status_code == 200:
                    _GITHUB_API_LAST_ERROR = ""
                    return response.json(), response.headers.get("Link", "")
            detail = (response.text or "").strip().replace("\n", " ")[:160]
            _GITHUB_API_LAST_ERROR = f"HTTP {response.status_code}" + (
                f": {detail}" if detail else ""
            )
            return None, ""
    except Exception as exc:
        _GITHUB_API_LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return None, ""


def _github_token() -> str:
    import os
    import shutil
    import subprocess

    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    if shutil.which("gh"):
        try:
            out = subprocess.check_output(
                ["gh", "auth", "token"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
            return (out or "").strip()
        except Exception:
            return ""
    return ""
