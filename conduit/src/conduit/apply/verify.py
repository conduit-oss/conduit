"""Re-check a patched file with Conduit impact (scratch tree)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from conduit.apply.models import ImpactSite


def site_cleared_by_impact(
    root: Path,
    site: ImpactSite,
    patched: str,
    *,
    fixtures: Path | None = None,
    allow_network: bool = False,
) -> bool:
    """Return True when the call site is gone after re-running impact."""
    return not remaining_breaks(
        root, site, patched, fixtures=fixtures, allow_network=allow_network
    )


def remaining_breaks(
    root: Path,
    site: ImpactSite,
    patched: str,
    *,
    span: tuple[int, int] | None = None,
    fixtures: Path | None = None,
    allow_network: bool = False,
) -> list[tuple[str, str]]:
    """``(finding, symbol)`` pairs impact still reports for the original site or the rewritten span.

    Copies ``root`` to a scratch tree, writes ``patched`` over ``site.path``,
    then calls ``collect_impacts`` for ``site.dependency`` with the same
    from/to versions. A hit counts when it shares the original path and
    expression (line may shift after the edit), or when it falls inside
    ``span`` (1-based, inclusive): a break the rewrite itself introduced,
    such as a parameter the new release removed.
    """
    with tempfile.TemporaryDirectory(prefix="conduit-apply-recheck-") as tmp:
        scratch = Path(tmp) / "tree"
        shutil.copytree(
            root,
            scratch,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                ".pytest_cache",
                "node_modules",
            ),
        )
        target = scratch / site.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patched, encoding="utf-8")
        return _breaks_listed(
            scratch,
            site,
            span=span,
            fixtures=fixtures,
            allow_network=allow_network,
        )


def _breaks_listed(
    root: Path,
    site: ImpactSite,
    *,
    span: tuple[int, int] | None,
    fixtures: Path | None,
    allow_network: bool,
) -> list[tuple[str, str]]:
    from conduit.impact.run import collect_impacts
    from conduit.outside_in.bridge import build_service_map

    from conduit.outside_in.library_types import installed_version, types_from

    dep, pinned = site.dependency or "", site.from_version or ""
    typed = {dep: pinned} if dep and pinned and installed_version(dep) not in {"", pinned} else {}
    with types_from(typed):
        service_map, _blast = build_service_map(root)
    impacts, _diffed, _notes = collect_impacts(
        root,
        service_map,
        fixtures=fixtures,
        dep=site.dependency or None,
        to_version=site.to_version or None,
        from_version=site.from_version or None,
        allow_network=allow_network,
    )
    want_path = site.path.replace("\\", "/")
    want_expr = (site.expr or "").strip()
    found: list[tuple[str, str]] = []
    for item in impacts:
        if item.kind == "deprecated":
            continue
        for hit in item.sites:
            if hit.via_wrapper:
                continue
            hit_path = hit.path.replace("\\", "/")
            if hit_path != want_path:
                continue
            same = (hit.expr == want_expr) if want_expr else hit.line == site.line
            inside = span is not None and span[0] <= hit.line <= span[1]
            if same or inside:
                text = (f"{hit_path}:{hit.line} {hit.expr}: {item.change}", item.symbol)
                if text not in found:
                    found.append(text)
    if span is not None:
        found.extend(
            _unknown_keyword_breaks(service_map, site, want_path, span, fixtures, allow_network)
        )
    return found


def _unknown_keyword_breaks(
    service_map, site: ImpactSite, path: str, span: tuple[int, int], fixtures, allow_network
) -> list[tuple[str, str]]:
    """Keywords a call in the rewritten span passes that the new release's callable rejects.

    Covers callables that are new in that release (no diff entry to compare).
    """
    from conduit.apply.signatures import new_release_wheel, unknown_keywords

    calls = [
        chain
        for chain in service_map.chains
        if chain.call_rel.replace("\\", "/") == path
        and span[0] <= chain.call_lineno <= span[1]
        and not chain.via_wrapper
        and chain.kind.endswith("_call")
        and chain.site_keywords
    ]
    if not calls:
        return []
    wheel = new_release_wheel(
        site.dependency, site.to_version, fixtures=fixtures, allow_network=allow_network
    )
    found: list[tuple[str, str]] = []
    for chain in calls:
        owner, rejected = unknown_keywords(wheel, chain.sdk_method, chain.site_keywords)
        for name in rejected:
            text = (
                f"{path}:{chain.call_lineno} {chain.call_expr}: keyword {name} is not a "
                f"parameter of {owner} in {site.dependency} {site.to_version}",
                chain.sdk_method,
            )
            if text not in found:
                found.append(text)
    return found
