"""Where versioned OpenAPI documents live for outside REST hosts.

This is a location catalog (host → repo/path/stats file), not a list of
endpoint changes. Change data always comes from diffing two resolved specs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

#: Cache directory for downloaded OpenAPI documents.
_CACHE = Path("/tmp/conduit-openapi")


@dataclass(frozen=True)
class SpecSource:
    """How to resolve a from/to OpenAPI document for one outside service."""

    service_id: str
    kind: str
    #: ``owner/repo`` on GitHub.
    repo: str = ""
    #: Path inside the git repo (git_repo kind).
    path: str = ""
    #: Default branch tip when a ref cannot be resolved.
    default_branch: str = "main"
    #: ``tag`` (stripe-style ``vNNNN``) or ``commit_by_date``.
    ref_style: str = ""
    #: Stats file for Stainless-generated SDKs (``openapi_spec_url`` when present).
    stats_file: str = ".stats.yml"
    #: Human-readable note about what we verified in the wild.
    note: str = ""


#: Host / service → where a versioned OpenAPI history can be fetched.
#: Verified against the live repos (see docs/backtest.md).
SPEC_SOURCES: dict[str, SpecSource] = {
    "stripe": SpecSource(
        service_id="stripe",
        kind="git_repo",
        repo="stripe/openapi",
        path="openapi/spec3.json",
        default_branch="master",
        ref_style="tag",
        note="Tagged releases (vNNNN) each ship openapi/spec3.json.",
    ),
    "github": SpecSource(
        service_id="github",
        kind="git_repo",
        repo="github/rest-api-description",
        path="descriptions/api.github.com/api.github.com.json",
        default_branch="main",
        ref_style="commit_by_date",
        note="Continuous commits update the OpenAPI file; resolve by date.",
    ),
    "anthropic": SpecSource(
        service_id="anthropic",
        kind="stainless_sdk",
        repo="anthropics/anthropic-sdk-python",
        stats_file=".stats.yml",
        note=(
            "SDK tags through v1.0.0 expose openapi_spec_url in .stats.yml; "
            "later tags (e.g. v1.5.0+) only list configured_endpoints."
        ),
    ),
    "openai": SpecSource(
        service_id="openai",
        kind="git_repo",
        repo="openai/openai-openapi",
        path="openapi.yaml",
        default_branch="master",
        ref_style="tag",
        note=(
            "Repo has sparse tags (1.3.0, 2.0.0). openai-python ships "
            ".castiron.stats.yml with openapi_spec_hash but no openapi_spec_url."
        ),
    ),
    "cloudflare": SpecSource(
        service_id="cloudflare",
        kind="stainless_sdk",
        repo="cloudflare/cloudflare-python",
        stats_file=".stats.yml",
        note="Current tags only list configured_endpoints; no openapi_spec_url.",
    ),
}


def spec_source_for(service_id: str) -> SpecSource | None:
    return SPEC_SOURCES.get((service_id or "").lower())


def resolve_spec_pair(
    service_id: str,
    *,
    from_ref: str,
    to_ref: str,
    allow_network: bool = True,
) -> tuple[Path | None, Path | None, str]:
    """Download the from/to OpenAPI documents for ``service_id``.

    ``from_ref`` / ``to_ref`` are API versions, SDK versions, ISO dates
    (``YYYY-MM-DD``), or git tags/SHAs depending on the source kind.

    Returns ``(old_path, new_path, note)``. Paths are set when both documents
    were fetched. ``note`` explains a miss when either side is missing.
    """
    source = spec_source_for(service_id)
    if source is None:
        return None, None, (
            f"not compared: no versioned OpenAPI source is catalogued for {service_id}"
        )
    if not allow_network:
        return None, None, (
            f"not compared: {service_id} has a versioned OpenAPI source "
            f"({_source_label(source)}) but network is disabled"
        )
    if not from_ref or not to_ref:
        return None, None, (
            f"not compared: {service_id} REST calls need from and to versions "
            f"to pick documents in {_source_label(source)}"
        )
    if source.kind == "git_repo":
        return _resolve_git_pair(source, from_ref=from_ref, to_ref=to_ref)
    if source.kind == "stainless_sdk":
        return _resolve_stainless_pair(source, from_ref=from_ref, to_ref=to_ref)
    return None, None, f"not compared: unknown OpenAPI source kind {source.kind!r}"


def _source_label(source: SpecSource) -> str:
    if source.kind == "git_repo":
        return f"{source.repo}/{source.path}"
    if source.kind == "stainless_sdk":
        return f"{source.repo}/{source.stats_file}"
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
        # Prefer the commit that last touched the spec at/before the date. Tags
        # are accepted above when the caller passes ``vNNNN`` directly.
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
        # Allow passing a branch tip / tag directly.
        if _raw_exists(source.repo, token, source.path):
            return token, ""
        return "", f"not a date or commit ref: {token!r}"
    sha = _commit_before(source.repo, source.path, date)
    if not sha:
        return "", f"no commit for {source.path} at or before {token}"
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
    tag = version if version.startswith("v") else f"v{version.lstrip('v')}"
    # Prefer the tag as given, then with/without v.
    for candidate in (version, tag, version.lstrip("v")):
        text = _fetch_text(
            f"https://raw.githubusercontent.com/{source.repo}/{quote(candidate, safe='')}/"
            f"{source.stats_file}"
        )
        if text is None:
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return "", f"{candidate}: stats file is not YAML"
        if not isinstance(data, dict):
            return "", f"{candidate}: stats file is empty"
        url = str(data.get("openapi_spec_url") or "").strip()
        if url:
            return url, ""
        return "", (
            f"{candidate}: {source.stats_file} has no openapi_spec_url "
            f"(keys: {', '.join(sorted(str(k) for k in data.keys()))})"
        )
    return "", f"no {source.stats_file} at tags for {version!r}"


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
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "conduit-openapi",
                },
            )
            if response.status_code != 200:
                return None, ""
            return response.json(), response.headers.get("Link", "")
    except Exception:
        return None, ""

