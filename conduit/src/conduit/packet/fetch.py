"""Download a published migration packet from http(s)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from conduit.packet.cache import packets_dir, save_packet

MAX_PACKET_BYTES = 2 * 1024 * 1024

# Env: base URL for public catalog (no trailing slash), e.g.
# https://raw.githubusercontent.com/conduit-oss/conduit-packets/main
CATALOG_BASE_ENV = "CONDUIT_PACKET_CATALOG_BASE"

_KNOWN_ECOSYSTEMS = ("pypi", "npm", "go", "maven", "other")


class PacketFetchError(Exception):
    """URL packet could not be downloaded or parsed."""


def is_packet_url(raw: str) -> bool:
    text = (raw or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def catalog_base_url() -> str | None:
    raw = (os.environ.get(CATALOG_BASE_ENV) or "").strip().rstrip("/")
    return raw or None


def catalog_url_for_packet(packet: dict[str, Any]) -> str | None:
    """
    Build ``{BASE}/by-package/{package}/{ecosystem}/{packet_id}.json`` from
    packet fields when ``CONDUIT_PACKET_CATALOG_BASE`` is set.
    """
    base = catalog_base_url()
    if not base:
        return None
    package = str(packet.get("package") or "").strip()
    ecosystem = str(packet.get("ecosystem") or "").strip().lower()
    packet_id = str(packet.get("packet_id") or "").strip()
    if not package or not ecosystem or not packet_id:
        return None
    slug = packet_id if packet_id.endswith(".json") else f"{packet_id}.json"
    return f"{base}/by-package/{package}/{ecosystem}/{slug}"


def catalog_url_for_name(
    name: str,
    *,
    package: str | None = None,
    ecosystem: str | None = None,
) -> str | None:
    """
    Build a catalog URL for a packet id / slug when CONDUIT_PACKET_CATALOG_BASE is set.

    Resolution order:
      1. Explicit ``package`` + ``ecosystem`` → by-package path (P4 slug WARN fix)
      2. ``{package}-{ecosystem}-{version}`` token in the name → by-package path
      3. Else root ``{name}.json`` (flat mirror written by ``packet publish`` for
         hop ids such as ``openai-0.28.1-1.0.0`` that omit the ecosystem token)

    Bare package names with no ``-`` (e.g. ``openai``) return None so callers
    fall through to detect synthesis instead of a bogus root URL.
    """
    base = catalog_base_url()
    if not base:
        return None
    slug = (name or "").strip()
    if not slug or is_packet_url(slug) or "/" in slug or "\\" in slug:
        return None
    if not slug.endswith(".json"):
        slug_file = f"{slug}.json"
    else:
        slug_file = slug
        slug = slug[: -len(".json")]

    pkg = (package or "").strip() or None
    eco = (ecosystem or "").strip().lower() or None
    if pkg and eco:
        return f"{base}/by-package/{pkg}/{eco}/{slug_file}"

    for eco_tok in _KNOWN_ECOSYSTEMS:
        token = f"-{eco_tok}-"
        if token in slug:
            package_part, _, rest = slug.partition(token)
            if package_part and rest:
                return f"{base}/by-package/{package_part}/{eco_tok}/{slug_file}"

    # Bare package name (no version / hop markers) is not a catalog slug.
    if "-" not in slug:
        return None

    return f"{base}/{slug_file}"


def _filename_from_url(url: str) -> str | None:
    path = unquote(urlparse(url).path or "")
    name = Path(path).name
    if name.lower().endswith(".json") and name not in {".json", ""}:
        return name
    return None


def _cache_path(root: Path, url: str, packet: dict[str, Any]) -> Path:
    name = _filename_from_url(url)
    if not name:
        packet_id = str(packet.get("packet_id") or "").strip()
        safe = packet_id.replace("/", "_").replace("\\", "_").replace(" ", "")
        name = f"{safe or 'downloaded-packet'}.json"
        if not name.endswith(".json"):
            name += ".json"
    return packets_dir(root) / name


def fetch_packet_url(
    url: str,
    *,
    root: Path,
    refresh: bool = False,
    timeout: float = 30.0,
) -> Path:
    """GET a packet JSON and cache it under ``.conduit/packets/``."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PacketFetchError(f"Invalid packet URL: {url}")

    hinted = _filename_from_url(url)
    if hinted and not refresh:
        existing = packets_dir(root) / hinted
        if existing.is_file():
            try:
                data = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                return existing

    headers = {"Accept": "application/json, text/plain;q=0.9, */*;q=0.8"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url.strip(), headers=headers)
    except httpx.HTTPError as exc:
        raise PacketFetchError(f"Failed to fetch packet URL: {exc}") from exc

    if resp.status_code < 200 or resp.status_code >= 300:
        raise PacketFetchError(
            f"Packet URL returned HTTP {resp.status_code} for {url}"
        )

    length = resp.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > MAX_PACKET_BYTES:
        raise PacketFetchError(
            f"Packet URL is larger than {MAX_PACKET_BYTES} bytes"
        )

    content = resp.content
    if len(content) > MAX_PACKET_BYTES:
        raise PacketFetchError(
            f"Packet URL is larger than {MAX_PACKET_BYTES} bytes"
        )

    try:
        text = content.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PacketFetchError("Packet URL did not return JSON") from exc
    if not isinstance(data, dict):
        raise PacketFetchError("Packet URL JSON must be an object")
    if not str(data.get("package") or "").strip() and not str(
        data.get("packet_id") or ""
    ).strip():
        raise PacketFetchError("Packet JSON missing package / packet_id")

    dest = _cache_path(root, url, data)
    if dest.is_file() and not refresh:
        return dest
    save_packet(dest, data)
    return dest
