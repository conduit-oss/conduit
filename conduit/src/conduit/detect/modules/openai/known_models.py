"""Dynamic known-model universe for client discovery (no static prefix allowlist)."""

from __future__ import annotations

import json
import os
import re
from typing import Iterable

import httpx

from conduit.detect.modules.openai.model_docs import fetch_models_catalog
from conduit.detect.modules.openai.models_legacy import ChangeType
from conduit.detect.modules.openai.workers.base import fixtures_dir

_MODELS_URL = "https://api.openai.com/v1/models"
_DEPRECATIONS_URL = "https://platform.openai.com/docs/deprecations"

_MODEL_KWARG_RE = re.compile(
    r"""(?:\bmodel\s*=\s*|['"]model['"]\s*:\s*)['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


def _is_model_id_char(c: str) -> bool:
    return c.isalnum() or c in "._-"


def _ids_from_deprecation_html(html: str) -> set[str]:
    from conduit.detect.modules.openai.workers.deprecation_scraper import (
        parse_deprecation_html,
    )

    out: set[str] = set()
    for signal in parse_deprecation_html(html, _DEPRECATIONS_URL):
        if signal.change_type != ChangeType.MODEL_DEPRECATION:
            continue
        for token in (signal.affected_pattern, signal.replacement_pattern):
            if not token or token.startswith("/"):
                continue
            out.add(token)
    return out


def _ids_from_live_models_payload(payload: dict) -> set[str]:
    return {
        str(item["id"])
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }


def collect_known_model_ids(
    *,
    demo: bool = False,
    include_api_models: bool = True,
) -> set[str]:
    """
    Build the known model-id universe from catalog + deprecations (+ optional /v1/models).

    Ids come from vendor sources Conduit already trusts — not a hardcoded prefix list.
    """
    ids: set[str] = set()

    if demo:
        catalog_path = fixtures_dir() / "model_docs" / "models.md"
        if catalog_path.is_file():
            ids.update(fetch_models_catalog(text=catalog_path.read_text(encoding="utf-8")))
        dep_path = fixtures_dir() / "deprecations" / "openai_deprecations.html"
        if dep_path.is_file():
            ids.update(_ids_from_deprecation_html(dep_path.read_text(encoding="utf-8")))
        if include_api_models:
            models_path = fixtures_dir() / "models" / "current_models.json"
            if models_path.is_file():
                try:
                    payload = json.loads(models_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    ids.update(_ids_from_live_models_payload(payload))
        return ids

    ids.update(fetch_models_catalog())
    try:
        resp = httpx.get(_DEPRECATIONS_URL, timeout=45.0, follow_redirects=True)
        resp.raise_for_status()
        ids.update(_ids_from_deprecation_html(resp.text))
    except (httpx.HTTPError, OSError):
        pass

    if include_api_models:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            try:
                resp = httpx.get(
                    _MODELS_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict):
                    ids.update(_ids_from_live_models_payload(payload))
            except (httpx.HTTPError, OSError, ValueError):
                pass

    return ids


def find_known_models_in_text(text: str, known: Iterable[str]) -> set[str]:
    """Verbatim (case-insensitive) hits of known ids; longest-first, non-overlapping."""
    ordered = sorted({k for k in known if k}, key=len, reverse=True)
    if not ordered or not text:
        return set()

    lower = text.lower()
    occupied = [False] * len(lower)
    # Map lower → first-seen canonical casing from *known*
    canon: dict[str, str] = {}
    for mid in known:
        if mid:
            canon.setdefault(mid.lower(), mid)

    found: set[str] = set()
    for mid in ordered:
        needle = mid.lower()
        start = 0
        while True:
            i = lower.find(needle, start)
            if i < 0:
                break
            j = i + len(needle)
            left_ok = i == 0 or not _is_model_id_char(lower[i - 1])
            right_ok = j >= len(lower) or not _is_model_id_char(lower[j])
            if left_ok and right_ok and not any(occupied[i:j]):
                for k in range(i, j):
                    occupied[k] = True
                found.add(canon.get(needle, mid))
            start = i + 1
    return found


def extract_model_kwarg_ids(text: str, known: Iterable[str]) -> set[str]:
    """model= / \"model\": string literals that appear in the known universe."""
    canon = {k.lower(): k for k in known if k}
    if not canon or not text:
        return set()
    out: set[str] = set()
    for match in _MODEL_KWARG_RE.finditer(text):
        token = match.group(1).strip()
        key = token.lower()
        if key in canon:
            out.add(canon[key])
    return out
