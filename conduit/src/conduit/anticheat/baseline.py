"""Pre-apply file text for soft-fail anticheat baseline comparisons."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

_BASELINE_NAME = "anticheat_baseline.json"


def baseline_path(root: Path) -> Path:
    return root.resolve() / ".conduit" / _BASELINE_NAME


def save_anticheat_baseline(root: Path, rels: Iterable[str], *, log=None) -> Path:
    """Snapshot current file text for ``rels`` under ``.conduit/anticheat_baseline.json``."""
    root = root.resolve()
    out: dict[str, str] = {}
    for raw in rels:
        rel = str(raw or "").replace("\\", "/").strip()
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
    """Load pre-apply baseline map; empty dict when missing or unreadable."""
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
        rel = str(key or "").replace("\\", "/").strip()
        if rel and isinstance(val, str):
            out[rel] = val
    return out
