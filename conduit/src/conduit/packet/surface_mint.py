"""Mint a surface packet from a producer package version (public exports only)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from conduit.export_delta.extract import extract_exports_from_tree
from conduit.export_delta.resolve import resolve_package_tree
from conduit.packet.surface_validate import validate_surface_packet

SourceKind = Literal["wheel", "tree", "git_tag"]


class SurfaceMintError(ValueError):
    """Producer surface could not be resolved or extracted."""


def _symbol_kind(symbol_id: str) -> str:
    if "." in symbol_id:
        return "method"
    return "export"


def _symbols_from_names(names: set[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for name in sorted(names):
        text = str(name).strip()
        if not text:
            continue
        out.append({"id": text, "kind": _symbol_kind(text)})
    return out


def canonical_surface_body(packet: dict[str, Any]) -> str:
    """Deterministic JSON for checksum (excludes checksum itself)."""
    body = {k: v for k, v in packet.items() if k != "checksum"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def checksum_surface_packet(packet: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_surface_body(packet).encode("utf-8")).hexdigest()


def surface_packet_id(*, ecosystem: str, package: str, version: str) -> str:
    return f"surface:{ecosystem}:{package}:{version}"


def mint_surface_packet_from_tree(
    tree: Path,
    *,
    package: str,
    version: str,
    ecosystem: str = "pypi",
    source_kind: SourceKind = "tree",
    locator: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Extract public exports from an unpacked tree into a surface packet."""
    package = (package or "").strip()
    version = str(version or "").strip()
    ecosystem = (ecosystem or "pypi").strip().lower() or "pypi"
    if not package or not version:
        raise SurfaceMintError("package and version are required")
    if not tree.is_dir():
        raise SurfaceMintError(f"tree is not a directory: {tree}")

    names = extract_exports_from_tree(tree, package=package, ecosystem=ecosystem)
    symbols = _symbols_from_names(names)
    if not symbols:
        raise SurfaceMintError(
            f"no public exports found for {package}=={version} under {tree}"
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
            "kind": source_kind,
            "locator": locator or str(tree.resolve()),
        },
        "symbols": symbols,
    }
    if notes:
        packet["notes"] = notes
    packet["checksum"] = checksum_surface_packet(packet)
    errors = validate_surface_packet(packet)
    if errors:
        raise SurfaceMintError("; ".join(errors))
    return packet


def mint_surface_packet_from_pypi(
    *,
    package: str,
    version: str,
    ecosystem: str = "pypi",
    cache_root: Path | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Resolve package==version into the export cache, then mint a surface packet."""
    tree, err = resolve_package_tree(
        package,
        version,
        ecosystem=ecosystem,
        cache_root=cache_root,
    )
    if tree is None:
        raise SurfaceMintError(err or f"failed to resolve {package}=={version}")
    return mint_surface_packet_from_tree(
        tree,
        package=package,
        version=version,
        ecosystem=ecosystem,
        source_kind="wheel",
        locator=f"{package}=={version}",
        notes=notes,
    )


def write_surface_packet(packet: dict[str, Any], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
