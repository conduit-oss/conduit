"""Watch outside services on a repo map. No internal repo-to-repo feeds."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from conduit.detect.manifests import pin_for_packet_ecosystem, read_installed_by_ecosystem
from conduit.feeds.catalog import feeds_for
from conduit.feeds.client import FeedClient
from conduit.feeds.model import FeedHit
from conduit.feeds.packet import packets_from_hits
from conduit.feeds.sources import FeedSource, default_sources
from conduit.feeds.targets import WatchTarget
from conduit.outside_in.bridge import build_service_map
from conduit.outside_in.catalog import catalog_spec
from conduit.outside_in.model import ServiceMap


@dataclass
class FeedWatch:
    """Packets produced for the outside services on one map."""

    services: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hits: list[FeedHit] = field(default_factory=list)
    packets: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "services": list(self.services),
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
            "packet_ids": [str(packet.get("packet_id") or "") for packet in self.packets],
            "packets": self.packets,
        }


def watch_outside(
    root: Path,
    *,
    client: FeedClient,
    service: str | None = None,
    sources: list[FeedSource] | None = None,
) -> FeedWatch:
    """Poll public feeds for catalogued services on ``root``'s map.

    Modules that are not outside services (local packages, pydantic, anything
    Pact / Optic / Backstage would cover) are skipped. ``sources`` replaces
    the built-in list when a caller wants a different plug-in set.
    """
    service_map, _blast = build_service_map(root, service=service)
    targets, skipped = targets_for_map(root, service_map, service=service)
    chosen = list(sources) if sources is not None else default_sources()
    hits: list[FeedHit] = []
    for target in targets:
        for source in chosen:
            try:
                hits.extend(source.collect(target, client))
            except Exception as exc:
                client.warnings.append(f"{source.name} {target.service_id}: {exc}")
    packets, problems = packets_from_hits(hits)
    return FeedWatch(
        services=[target.service_id for target in targets],
        skipped=skipped,
        warnings=[*client.warnings, *problems],
        hits=hits,
        packets=packets,
    )


def targets_for_map(
    root: Path,
    service_map: ServiceMap,
    *,
    service: str | None = None,
) -> tuple[list[WatchTarget], list[str]]:
    """Keep catalogued outside services. Drop everything else."""
    by_eco = read_installed_by_ecosystem(root)
    targets: list[WatchTarget] = []
    skipped: list[str] = []
    names = service_map.services()
    if service and not names:
        spec = catalog_spec(service)
        if spec is None:
            skipped.append(
                f"{service}: not an outside service; internal modules and "
                "repo-to-repo contracts are not watched"
            )
            return [], skipped
    for name in names:
        spec = catalog_spec(name)
        if spec is None:
            skipped.append(
                f"{name}: not an outside service; internal modules and "
                "repo-to-repo contracts are not watched"
            )
            continue
        feeds = feeds_for(spec.service_id)
        if feeds is None:
            skipped.append(f"{spec.service_id}: no public feeds configured")
            continue
        pypi_pin = pin_for_packet_ecosystem(by_eco, feeds.pypi or spec.package, "pypi")
        npm_pin = (
            pin_for_packet_ecosystem(by_eco, feeds.npm, "npm") if feeds.npm else None
        )
        targets.append(
            WatchTarget(
                service_id=spec.service_id,
                package=spec.package,
                pypi_version=pypi_pin,
                npm_version=npm_pin,
                feeds=feeds,
            )
        )
    return targets, skipped
