"""Cached HTTP for live feed reads.

Successful responses are kept in memory and under a temp directory.
Failures are not cached. GitHub hosts send ``GITHUB_TOKEN`` or ``GH_TOKEN``
when one is set. A 403 or 429 is retried once. Timeouts and rate limits
return a reason instead of raising.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

_MEMORY: dict[str, str] = {}
_CACHE = Path(tempfile.gettempdir()) / "conduit-feed-cache"
_RETRY_CAP = 2.0
_GITHUB_HOSTS = {
    "github.com",
    "www.github.com",
    "api.github.com",
    "raw.githubusercontent.com",
}


def clear_feed_cache() -> None:
    """Drop cached feed bodies. Tests call this between cases."""
    _MEMORY.clear()
    if not _CACHE.is_dir():
        return
    for child in _CACHE.iterdir():
        child.unlink(missing_ok=True)


def cached_get(url: str, *, timeout: float = 30.0) -> tuple[str | None, str]:
    """Return ``(body, reason)``. ``reason`` is empty when ``body`` is set."""
    if url in _MEMORY:
        return _MEMORY[url], ""
    path = _disk_path(url)
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text:
            _MEMORY[url] = text
            return text, ""
    body, reason = _request(url, timeout=timeout)
    if body is None:
        return None, reason or "could not be fetched"
    _MEMORY[url] = body
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError:
        pass
    return body, ""


def _disk_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _CACHE / digest


def _request(url: str, *, timeout: float) -> tuple[str | None, str]:
    import httpx

    headers = _headers(url)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            if response.status_code in {403, 429}:
                time.sleep(_retry_delay(response))
                response = client.get(url, headers=headers)
            if response.status_code >= 400:
                return None, f"HTTP {response.status_code}"
            return response.text, ""
    except httpx.TimeoutException:
        return None, "timed out"
    except httpx.HTTPError as exc:
        return None, str(exc) or "request failed"


def _headers(url: str) -> dict[str, str]:
    host = (urlparse(url).hostname or "").lower()
    headers = {"User-Agent": "conduit-watch"}
    if host == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if host in _GITHUB_HOSTS or host.endswith(".githubusercontent.com"):
        token = _token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _token() -> str:
    from conduit.impact.openapi_sources import _github_token

    return _github_token()


def _retry_delay(response: object) -> float:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after") or ""
    try:
        delay = float(raw) if raw else 1.0
    except (TypeError, ValueError):
        delay = 1.0
    if delay < 0:
        delay = 0.0
    return min(delay, _RETRY_CAP)
