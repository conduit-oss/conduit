"""Feed the outside-in graph into watch and the map command."""

from __future__ import annotations

from pathlib import Path

from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.blast import blast_radius
from conduit.outside_in.model import BlastReport, ServiceMap


def build_service_map(
    root: Path,
    *,
    service: str | None = None,
    packet: dict | None = None,
) -> tuple[ServiceMap, BlastReport | None]:
    extra: tuple[str, ...] = ()
    if packet is not None:
        package = str(packet.get("package") or "").strip()
        if package:
            extra = (package,)
    service_map = analyze_repo(root, service=service, extra_modules=extra)
    blast = blast_radius(service_map, packet) if packet is not None else None
    return service_map, blast
