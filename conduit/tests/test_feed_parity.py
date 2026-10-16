"""Live watch must find every break the recorded fixtures find.

The recorded set is a snapshot of public releases, written by
``scripts/record_fixtures.py``. The unit-test stub package is not part of
it. This calls the public internet. CI runs it in a separate job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conduit.feeds.client import FeedClient
from conduit.feeds.watch import FeedWatch, watch_outside

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "examples" / "service-clients"
FEEDS = REPO / "conduit" / "fixtures" / "feeds"
IMPACT = REPO / "conduit" / "fixtures" / "impact"
REPORT = Path("/tmp/conduit-feed-parity.json")


def _recorded() -> list[str]:
    payload = json.loads((IMPACT / "index.json").read_text(encoding="utf-8"))
    packages = payload.get("packages") if isinstance(payload, dict) else {}
    if not isinstance(packages, dict):
        return []
    return sorted(
        str(name)
        for name, entry in packages.items()
        if isinstance(entry, dict) and entry.get("recorded")
    )


def _breaks(result: FeedWatch, service: str) -> set[str]:
    key = service.lower()
    found: set[str] = set()
    for item in result.impacts:
        if str(item.get("dependency") or "").lower() == key:
            found.add(f"impact:{item.get('change')}")
    for hit in result.hits:
        if hit.change == "version":
            continue
        names = {hit.package.lower(), (hit.target or "").lower()}
        if key in names:
            found.add(f"hit:{hit.change}:{hit.old}->{hit.new}")
    return found


def _impact_count(result: FeedWatch, service: str) -> int:
    key = service.lower()
    return sum(1 for item in result.impacts if str(item.get("dependency") or "").lower() == key)


@pytest.mark.network
def test_live_watch_breaks_are_a_superset_of_recorded():
    recorded = _recorded()
    assert recorded, "no recorded fixtures; run scripts/record_fixtures.py"
    assert "acme-sdk" not in recorded
    offline = watch_outside(ROOT, client=FeedClient(root=FEEDS, allow_network=False))
    live = watch_outside(ROOT, client=FeedClient(allow_network=True))
    rows = []
    gaps = []
    for service in recorded:
        offline_breaks = _breaks(offline, service)
        live_breaks = _breaks(live, service)
        missing = sorted(offline_breaks - live_breaks)
        row = {
            "service": service,
            "offline_impacts": _impact_count(offline, service),
            "live_impacts": _impact_count(live, service),
            "offline_breaks": sorted(offline_breaks),
            "live_breaks": sorted(live_breaks),
            "missing": missing,
            "live_notes": [warning for warning in live.warnings if service.lower() in warning.lower()],
        }
        rows.append(row)
        if missing:
            gaps.append(row)
    REPORT.write_text(
        json.dumps({"rows": rows, "live_warnings": live.warnings}, indent=2) + "\n",
        encoding="utf-8",
    )
    assert not gaps, json.dumps(gaps, indent=2)
