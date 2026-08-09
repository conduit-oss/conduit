"""Build grounded evidence packs (seed URLs + link expand + web search)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

_MAX_SEED_PAGES = 8
_MAX_SEARCH_HITS = 5
_MAX_CHARS_PER_DOC = 12_000
_FETCH_TIMEOUT = 45.0


@dataclass
class EvidenceDoc:
    url: str
    title: str
    text: str
    kind: str  # seed | link | search


def host_allowed(url: str, allow_hosts: Iterable[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    allowed = {h.lower() for h in allow_hosts}
    # github.com only for /openai paths when github.com is allowlisted
    if host == "github.com":
        if "github.com" not in allowed:
            return False
        path = urlparse(url).path or ""
        return path == "/openai" or path.startswith("/openai/")
    return host in allowed


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = (soup.title.get_text(" ", strip=True) if soup.title else "") or ""
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text[:_MAX_CHARS_PER_DOC]


def _fetch_url(client: httpx.Client, url: str) -> EvidenceDoc | None:
    try:
        resp = client.get(url, follow_redirects=True)
        resp.raise_for_status()
    except (httpx.HTTPError, OSError):
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" not in ctype and not resp.text.lstrip().startswith("<"):
        text = resp.text[:_MAX_CHARS_PER_DOC]
        return EvidenceDoc(url=str(resp.url), title="", text=text, kind="seed")
    title, text = html_to_text(resp.text)
    return EvidenceDoc(url=str(resp.url), title=title, text=text, kind="seed")


def _extract_links(base_url: str, html: str, allow_hosts: Iterable[str]) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        abs_url = urljoin(base_url, href)
        if abs_url in seen:
            continue
        if not host_allowed(abs_url, allow_hosts):
            continue
        seen.add(abs_url)
        out.append(abs_url)
    return out


def web_search(query: str, *, max_results: int = _MAX_SEARCH_HITS) -> list[str]:
    """Return result URLs via ddgs. Empty list if package missing or search fails."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            return []
    urls: list[str] = []
    try:
        with DDGS() as ddgs:
            for hit in ddgs.text(query, max_results=max_results):
                href = str(hit.get("href") or hit.get("link") or "")
                if href:
                    urls.append(href)
    except Exception:
        return []
    return urls


def _search_host_ok(url: str, allow_hosts: Iterable[str], *, open_search: bool) -> bool:
    """When open_search=True, accept any http(s) URL from web search results."""
    if open_search:
        scheme = (urlparse(url).scheme or "").lower()
        host = (urlparse(url).hostname or "").lower()
        return scheme in {"http", "https"} and bool(host)
    return host_allowed(url, allow_hosts)


def build_evidence(
    *,
    seed_urls: list[str],
    allow_hosts: list[str],
    search_queries: list[str] | None = None,
    max_seed_pages: int = _MAX_SEED_PAGES,
    max_search_hits: int = _MAX_SEARCH_HITS,
    open_search: bool = False,
) -> tuple[list[EvidenceDoc], list[str]]:
    """
    Fetch seed pages, expand same-allowlist links, run web search.

    When open_search=True, search-hit URLs are not restricted to allow_hosts
    (seeds/links still are). Useful for repair-time general research.

    Returns (docs, warnings).
    """
    warnings: list[str] = []
    docs: list[EvidenceDoc] = []
    seen_urls: set[str] = set()
    link_queue: list[str] = []

    with httpx.Client(timeout=_FETCH_TIMEOUT, headers={"User-Agent": "conduit-evidence/0.3"}) as client:
        for url in seed_urls[:max_seed_pages]:
            if not host_allowed(url, allow_hosts):
                warnings.append(f"evidence skipped non-allowlisted seed: {url}")
                continue
            if url in seen_urls:
                continue
            try:
                resp = client.get(url, follow_redirects=True)
                resp.raise_for_status()
            except (httpx.HTTPError, OSError) as exc:
                warnings.append(f"evidence fetch failed {url}: {exc}")
                continue
            final = str(resp.url)
            seen_urls.add(url)
            seen_urls.add(final)
            title, text = html_to_text(resp.text) if "html" in (resp.headers.get("content-type") or "").lower() or resp.text.lstrip().startswith("<") else ("", resp.text[:_MAX_CHARS_PER_DOC])
            docs.append(EvidenceDoc(url=final, title=title, text=text, kind="seed"))
            for link in _extract_links(final, resp.text, allow_hosts):
                if link not in seen_urls:
                    link_queue.append(link)

        for link in link_queue:
            if len([d for d in docs if d.kind in {"seed", "link"}]) >= max_seed_pages:
                break
            if link in seen_urls:
                continue
            doc = _fetch_url(client, link)
            if not doc:
                continue
            doc.kind = "link"
            seen_urls.add(link)
            seen_urls.add(doc.url)
            docs.append(doc)

        search_urls: list[str] = []
        for query in search_queries or []:
            found = web_search(query, max_results=max_search_hits)
            if not found:
                warnings.append(f"evidence search returned 0 for: {query}")
            for href in found:
                if _search_host_ok(href, allow_hosts, open_search=open_search) and href not in seen_urls:
                    search_urls.append(href)

        # Allow more search pages when doing open/general research
        search_cap = max_search_hits * (3 if open_search else 1)
        for href in search_urls[:search_cap]:
            if href in seen_urls:
                continue
            doc = _fetch_url(client, href)
            if not doc:
                continue
            doc.kind = "search"
            seen_urls.add(href)
            seen_urls.add(doc.url)
            docs.append(doc)

    if not docs:
        warnings.append("evidence pack empty (no pages fetched)")
    return docs, warnings


def evidence_as_prompt_text(docs: list[EvidenceDoc], *, max_total_chars: int = 40_000) -> str:
    parts: list[str] = []
    used = 0
    for doc in docs:
        block = f"### {doc.kind.upper()} {doc.url}\nTitle: {doc.title}\n{doc.text}\n"
        if used + len(block) > max_total_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)
