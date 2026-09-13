"""Vendor pattern packs (hints for post-rule synthesis and seed rules)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "packs"


def pack_path(ecosystem: str, package: str) -> Path | None:
    eco = (ecosystem or "").strip().lower()
    pkg = (package or "").strip().lower().replace("-", "_")
    if not eco or not pkg:
        return None
    candidate = _PACKS_ROOT / eco / pkg / "patterns.yaml"
    if candidate.is_file():
        return candidate
    candidate = _PACKS_ROOT / f"{pkg}" / "patterns.yaml"
    if candidate.is_file():
        return candidate
    return None


def load_vendor_post_rules(
    packet: dict[str, Any],
) -> list[dict[str, Any]]:
    """Load seed post_rules from optional vendor pack."""
    eco = str(packet.get("ecosystem") or "")
    pkg = str(packet.get("package") or "")
    path = pack_path(eco, pkg)
    if path is None:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    rules = data.get("post_rules") or []
    out: list[dict[str, Any]] = []
    for rule in rules:
        if isinstance(rule, dict):
            item = dict(rule)
            item.setdefault("source", "vendor_pack")
            out.append(item)
    return out
