"""Run one coverage-grid cell: synthetic repo + fixture wheels + impact."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.impact.api import diff_wheels
from conduit.impact.intersect import intersect
from conduit.impact.run import collect_impacts
from conduit.outside_in.analyze import analyze_repo
from conduit.outside_in.evidence import classify_distribution

from .cells import Cell, expected_site, user_sources
from .library import (
    DIST,
    install_v1_into_repo,
    write_index,
    write_wheels,
)


def ensure_library_is_outside_service(tmp_path: Path) -> None:
    """Sanity: zzgrid classifies as an outside service via socket evidence."""
    repo = tmp_path / "classify"
    repo.mkdir(parents=True)
    install_v1_into_repo(repo)
    found = classify_distribution(DIST, "1.0.0", root=repo)
    assert found.service is True, f"zzgrid must classify as a service, got {found}"
    assert found.transport is False


def run_cell(tmp_path: Path, cell: Cell) -> dict:
    fixtures = tmp_path / "fixtures"
    wheels_dir = fixtures / "wheels"
    old, new = write_wheels(wheels_dir)
    write_index(fixtures, old, new)

    repo = tmp_path / "repo"
    repo.mkdir()
    install_v1_into_repo(repo)
    sources = user_sources(cell)
    for rel, text in sources.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")

    expected = expected_site(cell, sources)
    service_map = analyze_repo(repo)
    impacts, diffed, notes = collect_impacts(
        repo,
        service_map,
        fixtures=fixtures,
        dep=DIST,
        from_version="1.0.0",
        to_version="2.0.0",
        allow_network=False,
    )
    sites: list[tuple[str, int, str]] = []
    for impact in impacts:
        if impact.kind == "deprecated":
            continue
        for site in impact.sites:
            if site.via_wrapper:
                continue
            sites.append((site.path.replace("\\", "/"), int(site.line), impact.kind))

    flagged = False
    matched = None
    if expected is not None:
        exp_rel, exp_line = expected
        for path, line, kind in sites:
            if path.endswith(exp_rel) and line == exp_line:
                flagged = True
                matched = (path, line, kind)
                break
    else:
        flagged = False

    ok = flagged if cell.expect_flag else (not any(s[0].endswith("app.py") or True for s in sites) or not flagged)
    # For noflag cells: pass when the expected breaking line is absent from sites.
    if not cell.expect_flag:
        ok = True
        if expected is not None:
            exp_rel, exp_line = expected
            ok = not any(path.endswith(exp_rel) and line == exp_line for path, line, _ in sites)
        else:
            # default_changed: soft_default line should not be a hard break
            ok = not any("soft_default" in (impact.change or "") and impact.kind != "deprecated" for impact in impacts for _ in [0])
            # simpler: no non-deprecated site on soft_default call
            text = sources.get("app.py", "")
            soft_line = next(
                (i for i, line in enumerate(text.splitlines(), 1) if "soft_default" in line),
                None,
            )
            if soft_line is not None:
                ok = not any(line == soft_line and kind != "deprecated" for _, line, kind in sites)

    return {
        "id": cell.id,
        "binding": cell.binding,
        "reach": cell.reach,
        "change": cell.change,
        "expect_flag": cell.expect_flag,
        "expected": expected,
        "flagged": flagged,
        "matched": matched,
        "sites": sites,
        "diffed": sorted(diffed),
        "notes": notes,
        "ok": ok,
        "xfail_reason": cell.xfail_reason,
        "services": service_map.services(),
        "wheel_changes": len(diff_wheels(old, new)),
    }
