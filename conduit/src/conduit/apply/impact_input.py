"""Load call sites from existing impact JSON (no re-derivation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conduit.apply.models import ImpactSite


def load_impact_sites(path: Path) -> list[ImpactSite]:
    """Parse ``conduit impact --json`` output (or a list of impact dicts)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return sites_from_payload(raw)


def sites_from_payload(raw: Any) -> list[ImpactSite]:
    impacts: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key in ("impacts", "deprecations"):
            block = raw.get(key) or []
            if isinstance(block, list):
                impacts.extend(item for item in block if isinstance(item, dict))
        # Single impact object with sites.
        if not impacts and "sites" in raw:
            impacts = [raw]
    elif isinstance(raw, list):
        impacts = [item for item in raw if isinstance(item, dict)]
    else:
        raise ValueError("Impact JSON must be an object or a list of impacts")

    sites: list[ImpactSite] = []
    for item in impacts:
        dependency = str(item.get("dependency") or "")
        from_version = str(item.get("from_version") or "")
        to_version = str(item.get("to_version") or "")
        change = str(item.get("change") or "")
        confidence = str(item.get("confidence") or "")
        source = str(item.get("source") or "")
        kind = str(item.get("kind") or "")
        symbol = str(item.get("symbol") or "")
        old_signature = str(item.get("old") or item.get("old_signature") or "")
        new_signature = str(item.get("new") or item.get("new_signature") or "")
        new_docstring = str(item.get("new_docstring") or "")
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        for site in item.get("sites") or []:
            if not isinstance(site, dict):
                continue
            if site.get("via_wrapper"):
                # Primary SDK lines only; wrapper callers are not edit targets.
                continue
            path = str(site.get("path") or "").replace("\\", "/")
            line = int(site.get("line") or 0)
            if not path or line <= 0:
                continue
            sites.append(
                ImpactSite(
                    path=path,
                    line=line,
                    expr=str(site.get("expr") or ""),
                    dependency=dependency,
                    from_version=from_version,
                    to_version=to_version,
                    change=change,
                    confidence=confidence,
                    source=source,
                    kind=kind,
                    symbol=symbol,
                    old_signature=old_signature,
                    new_signature=new_signature,
                    new_docstring=new_docstring,
                    evidence=dict(evidence),
                )
            )
    return sites
