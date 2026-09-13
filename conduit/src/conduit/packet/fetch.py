"""Download a published migration packet from http(s)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from conduit.packet.cache import packets_dir, save_packet

MAX_PACKET_BYTES = 2 * 1024 * 1024


class PacketFetchError(Exception):
    """URL packet could not be downloaded or parsed."""


def is_packet_url(raw: str) -> bool:
    text = (raw or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


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
