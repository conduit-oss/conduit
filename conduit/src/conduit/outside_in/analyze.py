"""Language entry for outside-in analysis."""

from __future__ import annotations

from pathlib import Path

from conduit.outside_in.specs import ServiceSpec, evidence_spec, spec_for_name
from conduit.outside_in.distributions import expand_unknown_spec
from conduit.outside_in.graph import analyze_python
from conduit.outside_in.model import ServiceMap


def specs_for(
    *,
    service: str | None = None,
    extra_modules: tuple[str, ...] = (),
    root: Path | None = None,
) -> tuple[ServiceSpec, ...]:
    """One explicit service, or the services this repo actually reaches."""
    if service:
        found = evidence_spec(service, root=root)
        chosen = [found or spec_for_name(service)]
        # An explicit service still needs the HTTP libraries the repo imports,
        # so a URL aimed at that service is recognized as an HTTP call. Scope
        # the repo scan: only ``service`` is classified fully; other imports
        # stay shallow (own code + clear call-link delegates).
        if root is not None:
            from conduit.outside_in.evidence import specs_for_repo

            seen_ids = {item.service_id for item in chosen}
            for spec in specs_for_repo(root, focus=service):
                if spec.transport and spec.service_id not in seen_ids:
                    chosen.append(spec)
                    seen_ids.add(spec.service_id)
    elif root is not None:
        from conduit.outside_in.evidence import specs_for_repo

        chosen = list(specs_for_repo(root))
    else:
        chosen = []
    seen = {item.service_id for item in chosen}
    for name in extra_modules:
        spec = evidence_spec(name, root=root) or spec_for_name(name)
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
    only_files: list[str] | None = None,
) -> ServiceMap:
    """Inventory external services and trace them inward.

    Python is the only language pass. JS/TS belongs here as a second
    analyzer that appends entries and chains, not as a parallel report.
    ``only_files`` (repo-relative paths) limits the walk when a caller already
    knows which modules matter (apply recheck of one dependency). When set,
    discovery does not scan the whole tree for every installed distribution —
    only the named service / extra modules are classified.
    """
    if specs is not None:
        selected = specs
    elif only_files is not None:
        # Apply recheck: the dependency is already known. Skip specs_for_repo's
        # whole-tree import scan (it dominated per-site recheck time).
        chosen: list[ServiceSpec] = []
        seen: set[str] = set()
        for name in (*( [service] if service else () ), *extra_modules):
            if not name or name in seen:
                continue
            seen.add(name)
            chosen.append(evidence_spec(name, root=root) or spec_for_name(name))
        if service:
            chosen = [expand_unknown_spec(root, spec) for spec in chosen]
        selected = tuple(chosen)
    else:
        chosen = list(specs_for(service=service, extra_modules=extra_modules, root=root))
        if service:
            chosen = [expand_unknown_spec(root, spec) for spec in chosen]
        selected = tuple(chosen)
    service_map = analyze_python(root, selected, only_files=only_files)
    return _drop_unreached(service_map)


def _drop_unreached(service_map: ServiceMap) -> ServiceMap:
    """Hide a distribution that nothing calls.

    An import by itself is not usage. A call, constructor, webhook, or
    other chain is. HTTP transports stay in the spec list so URL calls
    can be attributed, and disappear here when no chain names them.
    """
    reached = {chain.service for chain in service_map.chains}
    entries = tuple(entry for entry in service_map.entries if entry.service in reached)
    evidence = {
        service: text
        for service, text in service_map.evidence.items()
        if service in reached
    }
    if len(entries) == len(service_map.entries) and evidence == service_map.evidence:
        return service_map
    return ServiceMap(
        root=service_map.root,
        entries=entries,
        chains=service_map.chains,
        limitations=service_map.limitations,
        evidence=evidence,
        unresolved_calls=service_map.unresolved_calls,
    )
