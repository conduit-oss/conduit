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
    """Return True when the call site is gone after re-running impact.

    Copies ``root`` to a scratch tree, writes ``patched`` over ``site.path``,
    then calls ``collect_impacts`` for ``site.dependency`` with the same
    from/to versions. The site is cleared when no remaining hit shares this
    path and expression (line may shift after the edit).
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
        return not _site_still_listed(
            scratch,
            site,
            fixtures=fixtures,
            allow_network=allow_network,
        )


def _site_still_listed(
    root: Path,
    site: ImpactSite,
    *,
    fixtures: Path | None,
    allow_network: bool,
) -> bool:
    from conduit.impact.run import collect_impacts
    from conduit.outside_in.bridge import build_service_map

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
    for item in impacts:
        if item.kind == "deprecated":
            continue
        for hit in item.sites:
            if hit.via_wrapper:
                continue
            hit_path = hit.path.replace("\\", "/")
            if hit_path != want_path:
                continue
            if want_expr and hit.expr == want_expr:
                return True
            if not want_expr and hit.line == site.line:
                return True
    return False
