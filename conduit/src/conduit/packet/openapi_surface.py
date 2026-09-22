"""Mint a surface packet from an OpenAPI 3.x document (paths + methods only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from conduit.packet.surface_mint import (
    SurfaceMintError,
    checksum_surface_packet,
    surface_packet_id,
)
from conduit.packet.surface_validate import validate_surface_packet

_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def load_openapi_document(path: Path) -> dict[str, Any]:
    """Parse YAML or JSON OpenAPI; root must be a mapping."""
    path = Path(path)
    if not path.is_file():
        raise SurfaceMintError(f"OpenAPI file not found: {path}")
    text = path.read_text(encoding="utf-8").lstrip("\ufeff").strip()
    if not text:
        raise SurfaceMintError(f"empty OpenAPI file: {path}")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"} or text[:1] not in "[{":
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise SurfaceMintError(f"OpenAPI root must be a mapping: {path}")
    return data


def symbols_from_openapi(doc: dict[str, Any]) -> list[dict[str, str]]:
    """
    Public surface symbols: stable ``METHOD /path`` ids from paths + methods.

    operationId is ignored for the id (kept only as notes elsewhere if needed);
    METHOD /path is the stable surface key across renames of operationId.
    """
    paths = doc.get("paths") or {}
    if not isinstance(paths, dict):
        return []
    ids: list[str] = []
    for raw_path, item in paths.items():
        path = str(raw_path or "").strip()
        if not path or not isinstance(item, dict):
            continue
        for method, op in item.items():
            key = str(method or "").strip().lower()
            if key not in _HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            ids.append(f"{key.upper()} {path}")
    return [{"id": sid, "kind": "export"} for sid in sorted(set(ids))]


def mint_surface_from_openapi(
    path: Path | str,
    *,
    package: str,
    version: str,
    ecosystem: str = "other",
    locator: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """
    Mint a surface_packet from an OpenAPI 3.x file.

    source.kind stays ``tree`` with locator pointing at the OpenAPI file path
    (schema allows only wheel|tree|git_tag).
    """
    package = (package or "").strip()
    version = str(version or "").strip()
    ecosystem = (ecosystem or "other").strip().lower() or "other"
    if not package or not version:
        raise SurfaceMintError("package and version are required")

    openapi_path = Path(path)
    doc = load_openapi_document(openapi_path)
    symbols = symbols_from_openapi(doc)
    if not symbols:
        raise SurfaceMintError(
            f"no path+method operations found in OpenAPI: {openapi_path}"
        )

    packet: dict[str, Any] = {
        "packet_kind": "surface",
        "packet_id": surface_packet_id(
            ecosystem=ecosystem, package=package, version=version
        ),
        "package": package,
        "ecosystem": ecosystem,
        "version": version,
        "source": {
            "kind": "tree",
            "locator": locator or str(openapi_path.resolve()),
        },
        "symbols": symbols,
    }
    if notes:
        packet["notes"] = notes
    else:
        packet["notes"] = f"OpenAPI surface from {openapi_path.name}"
    packet["checksum"] = checksum_surface_packet(packet)
    errors = validate_surface_packet(packet)
    if errors:
        raise SurfaceMintError("; ".join(errors))
    return packet
