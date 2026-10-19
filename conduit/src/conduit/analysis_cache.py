"""Disk cache for per-wheel analysis results.

Keys include a content digest of the analysis modules, so a code change
never reuses a stale entry. ``ANALYSIS_VERSION`` is an extra manual epoch
for intentional invalidation; prefer bumping it when analysis behaviour
changes outside the digested files. Override the root with
``CONDUIT_CACHE_DIR``, otherwise ``$XDG_CACHE_HOME/conduit`` or
``~/.cache/conduit``. Disable with ``CONDUIT_NO_CACHE=1`` or
``without_cache()`` / ``--no-cache``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

#: Manual epoch. Bump when analysis output changes without a digest file edit.
ANALYSIS_VERSION = "1"

#: Relative to the ``conduit`` package. Digest of these files is part of every key.
ANALYSIS_SOURCE_FILES: tuple[str, ...] = (
    "impact/api.py",
    "impact/intersect.py",
    "outside_in/library_types.py",
    "outside_in/evidence.py",
    "outside_in/graph.py",
    "analysis_cache.py",
)

_no_cache: ContextVar[bool] = ContextVar("conduit_no_cache", default=False)


def package_dir() -> Path:
    return Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def analysis_source_digest() -> str:
    """SHA-256 of the analysis modules that define cacheable output."""
    digest = hashlib.sha256()
    root = package_dir()
    for rel in ANALYSIS_SOURCE_FILES:
        path = root / rel
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def cache_version() -> str:
    """Version token embedded in every cache path and payload."""
    return f"{ANALYSIS_VERSION}-{analysis_source_digest()[:16]}"


def cache_enabled() -> bool:
    if _no_cache.get():
        return False
    flag = os.environ.get("CONDUIT_NO_CACHE", "").strip().lower()
    return flag not in {"1", "true", "yes", "on"}


@contextmanager
def without_cache() -> Iterator[None]:
    """Disable disk cache for the current context."""
    token = _no_cache.set(True)
    try:
        yield
    finally:
        _no_cache.reset(token)


def cache_root() -> Path:
    """User cache directory for Conduit analysis artefacts."""
    override = os.environ.get("CONDUIT_CACHE_DIR", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        if xdg:
            root = Path(xdg).expanduser() / "conduit"
        else:
            root = Path.home() / ".cache" / "conduit"
    root.mkdir(parents=True, exist_ok=True)
    return root


def file_sha256(path: Path) -> str:
    """Content hash of a wheel or other artefact."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def entry_path(kind: str, key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in key)
    if len(safe) > 180:
        safe = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_root() / kind / cache_version() / f"{safe}.json"


def read_json(kind: str, key: str) -> Any | None:
    """Return cached payload data, or None on miss / corruption / version skew."""
    if not cache_enabled():
        return None
    path = entry_path(kind, key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("analysis_version") != cache_version():
        return None
    if payload.get("kind") != kind:
        return None
    if "data" not in payload:
        return None
    return payload["data"]


#: Skip writing entries whose JSON would exceed this many bytes.
_MAX_ENTRY_BYTES = 32_000_000


def write_json(kind: str, key: str, data: Any) -> None:
    """Atomically write a cache entry. No-op when caching is disabled."""
    if not cache_enabled():
        return
    path = entry_path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "analysis_version": cache_version(),
        "kind": kind,
        "data": data,
    }
    try:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    if len(text) > _MAX_ENTRY_BYTES:
        return
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".conduit-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            Path(tmp_name).replace(path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError:
        return
