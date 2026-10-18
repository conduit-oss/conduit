"""Fetch feed bodies. Tests pass a fixture directory and never open a socket."""

from __future__ import annotations

import json
from pathlib import Path


class FeedOfflineError(RuntimeError):
    """No recorded fixture for this URL, and live fetches are disabled."""


class FeedClient:
    """Resolve a URL to text.

    With ``root`` pointing at a fixture directory, ``index.json`` maps each
    public URL to a file under that directory. ``allow_network`` is off in
    tests. Live watch sets it so a missing fixture can fall through to HTTP.
    """

    def __init__(self, *, root: Path | None = None, allow_network: bool = False) -> None:
        self.root = Path(root).resolve() if root is not None else None
        self.allow_network = allow_network
        self.warnings: list[str] = []
        self.index: dict[str, str] = {}
        if self.root is not None:
            index_path = self.root / "index.json"
            if index_path.is_file():
                payload = json.loads(index_path.read_text(encoding="utf-8"))
                files = payload.get("files") if isinstance(payload, dict) else None
                if isinstance(files, dict):
                    self.index = {str(k): str(v) for k, v in files.items()}

    def get_text(self, url: str) -> str:
        rel = self.index.get(url)
        if rel and self.root is not None:
            path = self.root / rel
            if path.is_file():
                return path.read_text(encoding="utf-8")
            self.warnings.append(f"fixture missing for {url}: {rel}")
            raise FeedOfflineError(url)
        if not self.allow_network:
            self.warnings.append(f"offline: no fixture for {url}")
            raise FeedOfflineError(url)
        return self._live(url)

    def _live(self, url: str) -> str:
        from conduit.feeds.http import cached_get

        body, reason = cached_get(url)
        if body is None:
            self.warnings.append(
                f"not compared: {url} could not be fetched ({reason or 'unknown'})"
            )
            raise FeedOfflineError(url)
        return body
