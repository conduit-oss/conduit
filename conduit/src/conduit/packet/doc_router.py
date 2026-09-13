"""Route official doc indexes (llms.txt) to relevant migration pages."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable
from urllib.parse import urlparse

import httpx

_LLMS_INDEX_URLS = (
    "https://developers.openai.com/api/llms.txt",
    "https://developers.openai.com/api/docs/llms.txt",
    "https://developers.openai.com/api/reference/llms.txt",
)

_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_./-]{2,}")

_SCORE_KEYWORDS = (
    "deprecat",
    "complet",
    "chat",
    "assist",
    "fine-tun",
    "fine_tun",
    "embed",
    "moderat",
    "migrat",
    "python",
    "parameter",
    "temperature",
    "max_tokens",
    "max_completion",
    "engine",
    "edit",
    "response",
    "sdk",
    "endpoint",
    "model",
    "legacy",
    "replacement",
    "successor",
)

_FETCH_TIMEOUT = 30.0


@lru_cache(maxsize=4)
def _fetch_index(url: str) -> str:
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT, headers={"User-Agent": "conduit-doc-router/0.1"}) as client:
            resp = client.get(url, follow_redirects=True)
            resp.raise_for_status()
            return resp.text
    except (httpx.HTTPError, OSError):
        return ""


def parse_llms_links(text: str) -> list[tuple[str, str]]:
    """Return (title, url) pairs from an llms.txt markdown index."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for title, url in _LINK_RE.findall(text or ""):
        url = url.strip()
        if url in seen:
            continue
        seen.add(url)
        out.append((title.strip(), url))
    return out


def collect_index_links(*, extra_indexes: Iterable[str] | None = None) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in list(_LLMS_INDEX_URLS) + list(extra_indexes or []):
        for title, href in parse_llms_links(_fetch_index(url)):
            if href in seen:
                continue
            seen.add(href)
            links.append((title, href))
    return links


def _context_tokens(context: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for chunk in context:
        text = str(chunk or "").lower()
        for tok in _TOKEN_RE.findall(text):
            if len(tok) >= 3:
                tokens.add(tok)
        for kw in _SCORE_KEYWORDS:
            if kw in text:
                tokens.add(kw)
    return tokens


def score_link(title: str, url: str, *, context_tokens: set[str]) -> float:
    hay = f"{title} {url}".lower()
    score = 0.0
    for tok in context_tokens:
        if tok in hay:
            score += 2.0 if tok.startswith("/v1/") else 1.0
    for kw in _SCORE_KEYWORDS:
        if kw in hay and kw in context_tokens:
            score += 1.5
    host = (urlparse(url).hostname or "").lower()
    if host == "developers.openai.com":
        score += 0.5
    if "deprecat" in hay:
        score += 1.0
    if "migration" in hay or "guide" in hay:
        score += 0.5
    return score


def resolve_doc_urls(
    *,
    context_chunks: Iterable[str],
    profile_seeds: Iterable[str] | None = None,
    max_urls: int = 8,
) -> list[str]:
    """
    Score llms.txt child links against migration context and return top URLs.

    Always prepends high-value profile seeds (deprecations, models catalog) when
    present in profile_seeds.
    """
    ctx = _context_tokens(context_chunks)
    ranked: list[tuple[float, str]] = []
    for title, url in collect_index_links():
        ranked.append((score_link(title, url, context_tokens=ctx), url))
    ranked.sort(key=lambda x: (-x[0], x[1]))

    out: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        u = str(url or "").strip()
        if not u or u in seen:
            return
        seen.add(u)
        out.append(u)

    priority = list(profile_seeds or [])
    for seed in priority:
        low = seed.lower()
        if any(k in low for k in ("deprecat", "llms.txt", "models", "migration", "changelog")):
            _add(seed)
    for _score, url in ranked:
        if len(out) >= max_urls:
            break
        _add(url)
    for seed in priority:
        if len(out) >= max_urls:
            break
        _add(seed)
    return out[:max_urls]
