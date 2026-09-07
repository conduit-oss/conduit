"""Load vendor impact catalogs from conduit/packs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from conduit.patcher.post_rules.vendor import pack_path


def load_vendor_impacts(packet: dict[str, Any]) -> dict[str, Any]:
    """Load impacts.yaml for packet ecosystem/package."""
    eco = str(packet.get("ecosystem") or "")
    pkg = str(packet.get("package") or "")
    path = pack_path(eco, pkg)
    if path is None:
        return {}
    impacts_path = path.parent / "impacts.yaml"
    if not impacts_path.is_file():
        return {}
    try:
        data = yaml.safe_load(impacts_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def impact_classes(packet: dict[str, Any]) -> list[dict[str, Any]]:
    raw = load_vendor_impacts(packet).get("impact_classes") or []
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def default_banned_kwargs_on(packet: dict[str, Any]) -> dict[str, list[str]]:
    raw = load_vendor_impacts(packet).get("default_banned_kwargs_on") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for callee, kwargs in raw.items():
        if isinstance(kwargs, list):
            out[str(callee)] = [str(k) for k in kwargs if k]
    return out
