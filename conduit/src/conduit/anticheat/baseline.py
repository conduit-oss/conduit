"""Pre-apply file text for soft-fail anticheat baseline comparisons."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

_BASELINE_NAME = "anticheat_baseline.json"


def baseline_path(root: Path) -> Path:
    return root.resolve() / ".conduit" / _BASELINE_NAME


def _repo_rel(root: Path, raw: str | Path) -> str | None:
    """Normalize ``raw`` to a repo-relative POSIX path under ``root``."""
    root = root.resolve()
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
    except OSError:
        return None
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        # Already looked like a relative key (e.g. podcast_ingest.py)
        if not Path(text).is_absolute():
            return text.lstrip("./")
        return None


def save_anticheat_baseline(root: Path, rels: Iterable[str], *, log=None) -> Path:
    """Snapshot current file text for ``rels`` under ``.conduit/anticheat_baseline.json``.

    Keys are always repo-relative POSIX paths so verify scans can look them up.
    """
    root = root.resolve()
    out: dict[str, str] = {}
    for raw in rels:
        rel = _repo_rel(root, raw)
        if not rel:
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
    dest = baseline_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    if log:
        log(f"[anticheat] baseline snapshot: {len(out)} file(s) → {dest.relative_to(root)}")
    return dest


def load_anticheat_baseline(root: Path) -> dict[str, str]:
    """Load pre-apply baseline map; empty dict when missing or unreadable.

    Absolute keys from older snapshots are rewritten to repo-relative paths.
    """
    root = root.resolve()
    path = baseline_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in data.items():
        if not isinstance(val, str):
            continue
        rel = _repo_rel(root, key)
        if rel:
            out[rel] = val
    return out
