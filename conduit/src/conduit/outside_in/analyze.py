"""Language entry for outside-in analysis."""

from __future__ import annotations

from pathlib import Path

from conduit.outside_in.catalog import ServiceSpec, spec_for_name
from conduit.outside_in.catalog import CATALOG
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
    selected = specs if specs is not None else specs_for(
        service=service, extra_modules=extra_modules
    )
    return analyze_python(root, selected)
