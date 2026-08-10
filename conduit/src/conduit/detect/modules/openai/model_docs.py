"""Parse OpenAI model catalog / per-model docs for supported endpoints."""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urljoin

import httpx

MODELS_CATALOG_URL = "https://developers.openai.com/api/docs/models.md"
MODEL_DOC_URL_TMPL = "https://developers.openai.com/api/docs/models/{model_id}.md"

_FETCH_TIMEOUT = 30.0
_USER_AGENT = "conduit-model-docs/0.1"

# Client api_pattern tokens → OpenAI route keys (without leading slash).
_API_PATTERN_ROUTES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^chat\.completions$", re.I), "v1/chat/completions"),
    (re.compile(r"^ChatCompletion$", re.I), "v1/chat/completions"),
    (re.compile(r"^embeddings\.create$", re.I), "v1/embeddings"),
    (re.compile(r"^Completion\.create$", re.I), "v1/completions"),
]

_MODEL_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(/api/docs/models/([a-zA-Z0-9][a-zA-Z0-9._-]*?)(?:\.md)?\)"
)
_MODEL_ID_IN_CODE_RE = re.compile(r"`([a-zA-Z0-9._-]{3,})`")
_ENDPOINT_ROW_RE = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*`?(v1/[a-z0-9/_-]+)`?\s*\|\s*(Supported|Not supported)\s*\|",
    re.I | re.M,
)


def normalize_route(route: str) -> str:
    """Normalize to `v1/...` without a leading slash."""
    r = (route or "").strip().lstrip("/")
    if r.startswith("v1/"):
        return r.lower()
    if r.startswith("/v1/"):
        return r[1:].lower()
    return r.lower()


def model_doc_url(model_id: str) -> str:
    return MODEL_DOC_URL_TMPL.format(model_id=model_id.strip())


def map_api_patterns_to_routes(api_patterns: Iterable[str]) -> list[str]:
    """Map client-scanned api tokens to required endpoint routes."""
    required: set[str] = set()
    for raw in api_patterns:
        token = str(raw or "").strip()
        if not token:
            continue
        if token.startswith("/v1/") or token.startswith("v1/"):
            required.add(normalize_route(token))
            continue
        for pattern, route in _API_PATTERN_ROUTES:
            if pattern.match(token):
                required.add(route)
                break
    return sorted(required)


def parse_endpoints_markdown(text: str) -> dict[str, bool]:
    """Parse Endpoints table from a model `.md` page → route → supported."""
    endpoints: dict[str, bool] = {}
    for match in _ENDPOINT_ROW_RE.finditer(text or ""):
        route = normalize_route(match.group(2))
        supported = match.group(3).strip().lower() == "supported"
        endpoints[route] = supported
    return endpoints


def parse_models_catalog(text: str) -> list[str]:
    """Extract model ids from the models catalog markdown."""
    seen: set[str] = set()
    out: list[str] = []
    for _label, model_id in _MODEL_LINK_RE.findall(text or ""):
        mid = model_id.strip()
        if mid.lower().endswith(".md"):
            mid = mid[: -len(".md")]
        if mid.lower() in {"models", "deprecations"} or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    # Fallback: fenced ids that look like model names
    if not out:
        for mid in _MODEL_ID_IN_CODE_RE.findall(text or ""):
            if mid in seen or not re.search(r"[a-zA-Z]", mid):
                continue
            if mid.count("-") < 1 and not mid.startswith(("gpt", "o", "text", "tts", "whisper")):
                continue
            seen.add(mid)
            out.append(mid)
    return out


def model_supports_routes(
    endpoints: dict[str, bool] | None, required: Iterable[str]
) -> bool | None:
    """
    Whether *endpoints* covers every required route.

    Returns:
      True  — no requirements, or all required routes are Supported
      False — docs are known and at least one required route is missing/unsupported
      None  — endpoints unknown (fetch/parse failure); do not treat as unsupported
    """
    req = [normalize_route(r) for r in required]
    if not req:
        return True
    if not endpoints:
        return None
    return all(endpoints.get(r, False) for r in req)


def unsupported_routes(
    endpoints: dict[str, bool] | None, required: Iterable[str]
) -> list[str]:
    """Routes from *required* that are missing or Not supported. Empty if docs unknown."""
    if not endpoints:
        return []
    missing: list[str] = []
    for r in required:
        route = normalize_route(r)
        if not endpoints.get(route, False):
            missing.append(route)
    return missing


def _fetch_text(url: str, *, client: httpx.Client | None = None) -> str | None:
    own = client is None
    http = client or httpx.Client(
        timeout=_FETCH_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    try:
        resp = http.get(url)
        resp.raise_for_status()
        return resp.text
    except (httpx.HTTPError, OSError):
        return None
    finally:
        if own:
            http.close()


def fetch_models_catalog(
    *,
    url: str = MODELS_CATALOG_URL,
    text: str | None = None,
    client: httpx.Client | None = None,
) -> list[str]:
    body = text if text is not None else _fetch_text(url, client=client)
    if body is None:
        return []
    return parse_models_catalog(body)


def fetch_model_endpoints(
    model_id: str,
    *,
    text: str | None = None,
    client: httpx.Client | None = None,
    base_url: str | None = None,
) -> dict[str, bool] | None:
    """
    Load Supported-endpoints map for a model.

    Returns None when the page cannot be fetched or no endpoints table is parsed
    (unknown — not the same as “supports nothing”).
    """
    if text is None:
        url = model_doc_url(model_id) if not base_url else urljoin(base_url, f"{model_id}.md")
        text = _fetch_text(url, client=client)
    if text is None:
        return None
    parsed = parse_endpoints_markdown(text)
    if not parsed:
        return None
    return parsed


def prefer_catalog_candidates(
    catalog: list[str],
    *,
    required_routes: list[str],
    rejected: str | None = None,
    limit: int = 5,
) -> list[str]:
    """Rank catalog ids for alternate search (chat-friendly first when needed)."""
    need_chat = "v1/chat/completions" in {normalize_route(r) for r in required_routes}
    preferred_prefixes = (
        "gpt-5.6",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5",
        "gpt-4o",
        "gpt-4.1",
        "gpt-4",
        "o4",
        "o3",
        "o1",
    )
    scored: list[tuple[int, str]] = []
    for mid in catalog:
        if rejected and mid.lower() == rejected.lower():
            continue
        score = 100
        lower = mid.lower()
        if need_chat:
            if any(x in lower for x in ("embed", "whisper", "tts", "transcribe", "moderation", "realtime", "image", "sora", "audio")):
                score += 50
            for i, prefix in enumerate(preferred_prefixes):
                if lower.startswith(prefix):
                    score = i
                    break
        scored.append((score, mid))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [m for _, m in scored[:limit]]
