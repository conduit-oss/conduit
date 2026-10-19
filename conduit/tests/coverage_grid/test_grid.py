"""Execute the coverage grid; xfail known gaps so CI stays green."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .cells import Cell, build_cells
from .harness import ensure_library_is_outside_service, run_cell
from .library import DIST

_CELLS = build_cells()
_ROOT = Path(__file__).resolve().parents[3]
_DOC = _ROOT / "docs" / "coverage-grid.md"
_RESULTS = Path("/tmp/conduit-jobs/coverage-grid-results.json")


def test_zzgrid_classifies_as_outside_service(tmp_path: Path) -> None:
    ensure_library_is_outside_service(tmp_path)


def _params() -> list:
    found = []
    for cell in _CELLS:
        mark = pytest.mark.xfail(strict=False, reason=cell.xfail_reason) if cell.xfail_reason else None
        found.append(pytest.param(cell, id=cell.id, marks=[mark] if mark else []))
    return found


@pytest.mark.parametrize("cell", _params())
def test_coverage_cell(cell: Cell, tmp_path: Path) -> None:
    result = run_cell(tmp_path, cell)
    assert DIST in result["services"] or result["services"] == [DIST] or DIST in result["services"], (
        f"zzgrid not mapped: services={result['services']} notes={result['notes']}"
    )
    # Persist for the markdown matrix writer (last run wins per id via session).
    _RESULTS.parent.mkdir(parents=True, exist_ok=True)
    blob = {}
    if _RESULTS.exists():
        try:
            blob = json.loads(_RESULTS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            blob = {}
    rows = {row["id"]: row for row in blob.get("rows", [])}
    rows[cell.id] = {
        "id": cell.id,
        "binding": cell.binding,
        "reach": cell.reach,
        "change": cell.change,
        "expect_flag": cell.expect_flag,
        "ok": result["ok"],
        "flagged": result["flagged"],
        "xfail_reason": cell.xfail_reason,
        "expected": list(result["expected"]) if result["expected"] else None,
        "sites": result["sites"][:5],
    }
    payload = {"rows": list(rows.values())}
    _RESULTS.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if cell.xfail_reason:
        # Known gap: pass silently when Conduit still misses; xfail-pass is OK
        # (strict=False) so a future fix just flips the cell to a normal pass.
        if result["ok"]:
            return
        pytest.xfail(cell.xfail_reason)
    assert result["ok"], (
        f"cell {cell.id} failed: expect_flag={cell.expect_flag} flagged={result['flagged']} "
        f"expected={result['expected']} sites={result['sites'][:8]} notes={result['notes'][:2]}"
    )


def test_write_coverage_grid_doc(tmp_path: Path) -> None:
    """Run every cell once and write docs/coverage-grid.md (summary for CI/humans)."""
    rows = []
    for cell in _CELLS:
        result = run_cell(tmp_path / cell.id.replace(":", "_").replace("|", "_"), cell)
        rows.append(
            {
                "id": cell.id,
                "binding": cell.binding,
                "reach": cell.reach,
                "change": cell.change,
                "expect_flag": cell.expect_flag,
                "ok": result["ok"],
                "xfail": bool(cell.xfail_reason),
                "xfail_reason": cell.xfail_reason,
                "flagged": result["flagged"],
                "root_cause": (
                    cell.xfail_reason
                    if cell.xfail_reason and not result["ok"]
                    else ("" if result["ok"] else "unexpected miss/false flag")
                ),
            }
        )
    passed = sum(1 for r in rows if r["ok"] and not r["xfail"])
    xfailed = sum(1 for r in rows if r["xfail"])
    failed = sum(1 for r in rows if not r["ok"] and not r["xfail"])
    lines = [
        "# Coverage grid",
        "",
        f"Synthetic outside-service library `{DIST}` (socket client) × binding × reach × change.",
        "",
        f"- cells: **{len(rows)}**",
        f"- pass: **{passed}**",
        f"- xfail (known gap): **{xfailed}**",
        f"- fail: **{failed}**",
        "",
        "| id | expect | result | note |",
        "|----|--------|--------|------|",
    ]
    for row in rows:
        if row["ok"] and not row["xfail"]:
            status = "pass"
        elif row["xfail"]:
            status = "xfail" if not row["ok"] else "xfail-pass"
        else:
            status = "FAIL"
        note = row["xfail_reason"] or row["root_cause"] or ""
        lines.append(
            f"| `{row['id']}` | {'flag' if row['expect_flag'] else 'noflag'} | {status} | {note} |"
        )
    lines.append("")
    lines.append("## Failing / xfail categories")
    lines.append("")
    from collections import Counter

    cats = Counter(
        (r["xfail_reason"] or r["root_cause"] or "ok")
        for r in rows
        if not r["ok"] or r["xfail"]
    )
    for reason, count in cats.most_common():
        lines.append(f"- ({count}) {reason}")
    lines.append("")
    _DOC.parent.mkdir(parents=True, exist_ok=True)
    _DOC.write_text("\n".join(lines), encoding="utf-8")
    assert failed == 0, f"{failed} unexpected failures; see {_DOC}"
