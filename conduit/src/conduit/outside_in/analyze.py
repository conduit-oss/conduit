"""Language entry for outside-in analysis."""

from __future__ import annotations

from pathlib import Path

from conduit.outside_in.catalog import ServiceSpec, spec_for_name
from conduit.outside_in.catalog import CATALOG
from conduit.outside_in.distributions import expand_unknown_spec, merge_declared_specs
from conduit.outside_in.graph import analyze_python
from conduit.outside_in.model import ServiceMap


def specs_for(
    *,
    service: str | None = None,
    extra_modules: tuple[str, ...] = (),
) -> tuple[ServiceSpec, ...]:
    """Catalog services, or one explicit service plus any extra module names."""
    if service:
        chosen = [spec_for_name(service)]
    else:
        chosen = list(CATALOG.values())
    seen = {item.service_id for item in chosen}
    for name in extra_modules:
        spec = spec_for_name(name)
        if spec.service_id not in seen:
            chosen.append(spec)
            seen.add(spec.service_id)
    return tuple(chosen)


def analyze_repo(
    root: Path,
    *,
    service: str | None = None,
    extra_modules: tuple[str, ...] = (),
    specs: tuple[ServiceSpec, ...] | None = None,
) -> ServiceMap:
    """Inventory external services and trace them inward.

    Python is the only language pass. JS/TS belongs here as a second
    analyzer that appends entries and chains, not as a parallel report.
    """
    generic_ids: set[str] = set()
    if specs is not None:
        selected = specs
    else:
        chosen = list(specs_for(service=service, extra_modules=extra_modules))
        if service:
            chosen = [expand_unknown_spec(root, spec) for spec in chosen]
        else:
            chosen, generic_ids = merge_declared_specs(root, chosen)
        selected = tuple(chosen)
    service_map = analyze_python(root, selected)
    if generic_ids:
        service_map = _drop_unreached(service_map, generic_ids)
    return service_map


def _drop_unreached(service_map: ServiceMap, generic_ids: set[str]) -> ServiceMap:
    """Hide a declared distribution that nothing calls.

    An import by itself is not usage. A call, constructor, webhook, or
    other chain is.
    """
    reached = {chain.service for chain in service_map.chains}
    entries = tuple(
        entry
        for entry in service_map.entries
        if entry.service not in generic_ids or entry.service in reached
    )
    if len(entries) == len(service_map.entries):
        return service_map
    return ServiceMap(
        root=service_map.root,
        entries=entries,
        chains=service_map.chains,
        limitations=service_map.limitations,
    )
