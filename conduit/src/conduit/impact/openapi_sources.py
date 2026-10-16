"""Discover versioned OpenAPI, Swagger, or discovery documents.

Change data comes from diffing two specs. There is no location table.

For a package:
  PyPI ``project_urls`` → source git repo → at each release tag, look for a
  committed OpenAPI, Swagger, or discovery document (common paths, generator
  inputs, a stats file's ``openapi_spec_url``, workflow inputs).
"""

from __future__ import annotations

import re
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




def resolve_spec_pair(
    service_id: str,
    *,
    from_ref: str,
    to_ref: str,
    allow_network: bool = True,
    package: str = "",
) -> tuple[Path | None, Path | None, str]:
    """Download the from/to API spec for ``package`` (or ``service_id``).

    ``from_ref`` and ``to_ref`` are release tags or versions. Spec discovery
    walks the package's source repository at those tags. There is no location
    table.

    Returns ``(old_path, new_path, note)``. Paths are set when both documents
    were fetched. ``note`` explains a miss when either side is missing.
    """
    pkg = (package or service_id or "").strip()
    if not allow_network:
        if pkg:
            return None, None, (
                f"not compared: spec discovery for {pkg} needs network"
            )
        return None, None, (
            f"not compared: spec discovery found no versioned API spec for {service_id}"
        )
    if not from_ref or not to_ref:
        return None, None, (
            f"not compared: {pkg or service_id} REST calls need from and to "
            f"versions to pick API spec documents"
        )
    if not pkg:
        return None, None, (
            f"not compared: spec discovery found no versioned API spec for {service_id}"
        )
    old, new, note = resolve_sdk_package_pair(
        pkg, from_ref=from_ref, to_ref=to_ref
    )
    if old is not None and new is not None:
        return old, new, note
    if "no source repo" not in note and "no OpenAPI" not in note:
        return old, new, note
    discovery_miss = (
        f"not compared: spec discovery found no versioned API spec for {service_id}"
    )
    return None, None, f"{note}; {discovery_miss}" if note else discovery_miss


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
