"""Watch outside services on a repo map. No internal repo-to-repo feeds."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from conduit.deps.manifests import pin_for_packet_ecosystem, read_installed_by_ecosystem
from conduit.feeds.lookup import feeds_for
from conduit.feeds.client import FeedClient
from conduit.feeds.model import FeedHit
from conduit.feeds.packet import packets_from_hits
from conduit.feeds.sources import FeedSource, default_sources
from conduit.feeds.targets import WatchTarget
from conduit.outside_in.bridge import build_service_map
from conduit.outside_in.specs import evidence_spec
from conduit.outside_in.model import ServiceMap


@dataclass
class FeedWatch:
    """Packets produced for the outside services on one map."""

    services: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hits: list[FeedHit] = field(default_factory=list)
    packets: list[dict] = field(default_factory=list)
    impacts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "services": list(self.services),
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
            "packet_ids": [str(packet.get("packet_id") or "") for packet in self.packets],
            "packets": self.packets,
            "impacts": self.impacts,
        }


def watch_outside(
    root: Path,
    *,
    client: FeedClient,
    service: str | None = None,
    sources: list[FeedSource] | None = None,
) -> FeedWatch:
    """Poll public feeds for outside services on ``root``'s map.

    Modules that are not outside services (local packages, pydantic, anything
    Pact / Optic / Backstage would cover) are skipped. ``sources`` replaces
    the built-in list when a caller wants a different plug-in set.
    """
    service_map, _blast = build_service_map(root, service=service)
    targets, skipped = targets_for_map(
        root,
        service_map,
        service=service,
        urls=client.index or None,
    )
    chosen = list(sources) if sources is not None else default_sources()
    hits: list[FeedHit] = []
    for target in targets:
        for source in chosen:
            try:
                hits.extend(source.collect(target, client))
            except Exception as exc:
                client.warnings.append(f"{source.name} {target.service_id}: {exc}")
    packets, problems = packets_from_hits(hits)
    impact_root = _impact_fixtures(client)
    impacts: list[dict] = []
    if impact_root is not None:
        from conduit.impact.report import impacts_to_dicts
        from conduit.impact.run import collect_impacts

        found, diffed, _notes = collect_impacts(
            root,
            service_map,
            fixtures=impact_root,
            hits=hits,
            allow_network=client.allow_network,
        )
        impacts = impacts_to_dicts(found)
        # A version number alone is not a notice. Once the released surface
        # was compared, drop a bump-only packet and keep the line-by-line impact.
        packets = [
            packet
            for packet in packets
            if not (_bump_only(packet) and str(packet.get("package") or "").lower() in diffed)
        ]
    return FeedWatch(
        services=[target.service_id for target in targets],
        skipped=skipped,
        warnings=[*client.warnings, *problems],
        hits=hits,
        packets=packets,
        impacts=impacts,
    )


def _impact_fixtures(client: FeedClient) -> Path | None:
    """Recorded wheels and schemas next to a feed fixture directory."""
    if client.root is None:
        return None
    for candidate in (client.root.parent / "impact", client.root / "impact"):
        if (candidate / "index.json").is_file():
            return candidate
    return None


def _bump_only(packet: dict) -> bool:
    rules = [rule for rule in (packet.get("rules") or []) if isinstance(rule, dict)]
    if not rules:
        return False
    if any(str(rule.get("type") or "") != "DEPENDENCY_BUMP" for rule in rules):
        return False
    return not packet.get("side_effects")


def targets_for_map(
    root: Path,
    service_map: ServiceMap,
    *,
    service: str | None = None,
    urls: dict[str, str] | None = None,
) -> tuple[list[WatchTarget], list[str]]:
    """Keep classified outside services. Drop everything else."""
    by_eco = read_installed_by_ecosystem(root)
    pins = {name.lower(): version for name, version in (by_eco.get("pypi") or {}).items()}
    targets: list[WatchTarget] = []
    skipped: list[str] = []
    names = service_map.services()
    if service and not names:
        spec = evidence_spec(service, root=root)
        if spec is None:
            skipped.append(
                f"{service}: not an outside service; internal modules and "
                "repo-to-repo contracts are not watched"
            )
            return [], skipped
    for name in names:
        spec = evidence_spec(name, pins.get(name.lower()), root=root)
        if spec is None:
            skipped.append(
                f"{name}: not an outside service; internal modules and "
                "repo-to-repo contracts are not watched"
            )
            continue
        feeds = feeds_for(spec.service_id, spec.package, urls=urls)
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
