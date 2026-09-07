"""Load vendor anti-cheat catalogs from conduit/packs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PACKS_ROOT = Path(__file__).resolve().parents[1] / "packs"


def pack_path(ecosystem: str, package: str) -> Path | None:
    eco = (ecosystem or "").strip().lower()
    pkg = (package or "").strip().lower().replace("-", "_")
    if not eco or not pkg:
        return None
    candidate = _PACKS_ROOT / eco / pkg / "patterns.yaml"
    if candidate.is_file():
        return candidate
    candidate = _PACKS_ROOT / pkg / "patterns.yaml"
    if candidate.is_file():
        return candidate
    return None


def load_vendor_anticheat(packet: dict[str, Any]) -> dict[str, Any]:
    """Load anticheat.yaml for packet ecosystem/package."""
    eco = str(packet.get("ecosystem") or "")
    pkg = str(packet.get("package") or "")
    path = pack_path(eco, pkg)
    if path is None:
        return {}
    anticheat_path = path.parent / "anticheat.yaml"
    if not anticheat_path.is_file():
        return {}
    try:
        data = yaml.safe_load(anticheat_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def cheat_kind_severity(packet: dict[str, Any], kind: str) -> str:
    """Return block or advisory for a cheat kind (default block)."""
    kind = (kind or "").strip().lower().replace(" ", "_")
    catalog = load_vendor_anticheat(packet).get("cheat_kinds") or {}
    if isinstance(catalog, dict):
        entry = catalog.get(kind)
        if isinstance(entry, dict):
            sev = str(entry.get("severity") or "block").strip().lower()
            if sev in {"advisory", "warn", "warning"}:
                return "advisory"
        elif isinstance(entry, str) and entry.strip().lower() in {
            "advisory",
            "warn",
            "warning",
        }:
            return "advisory"
    return "block"


def audit_exclude_globs(packet: dict[str, Any]) -> list[str]:
    raw = load_vendor_anticheat(packet).get("audit_scope") or {}
    if not isinstance(raw, dict):
        return []
    globs = raw.get("exclude_globs") or []
    return [str(g) for g in globs if g]


def legal_surface_hints(packet: dict[str, Any]) -> dict[str, Any]:
    raw = load_vendor_anticheat(packet).get("legal_surfaces") or {}
    return raw if isinstance(raw, dict) else {}
