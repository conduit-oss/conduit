"""Human checklist surfacing for mergeable PRs."""

from __future__ import annotations

from conduit.human_checklist import (
    build_human_checklist,
    format_human_checklist,
    format_human_checklist_markdown,
    format_side_effect_row,
)
from conduit.pr_generator import build_pr_body
from conduit.patcher.engine import PatchReport
from conduit.run_summary import build_run_summary, format_run_summary
from conduit.test_runner import TestResult


def _packet():
    return {
        "packet_id": "pydantic-1.10.13-2.0.0",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.10.13",
        "to_version": "2.0.0",
        "rules": [],
        "side_effects": [
            {
                "kind": "other",
                "detail": "Public export removed with no rename match: MAX_EMAIL_LENGTH",
                "gap_kind": "uncodable",
                "old_shape": "MAX_EMAIL_LENGTH",
                "blocker": "surface diff found no successor id",
            },
            {
                "kind": "other",
                "detail": "v1 always=True and each_item=True have no 1:1 map.",
                "gap_kind": "multi_step",
                "old_shape": "@validator(..., each_item=True)",
                "blocker": "detector refuses unmapped kwargs",
            },
        ],
    }


class _L:
    def __init__(self, text: str):
        self._t = text

    def display(self) -> str:
        return self._t


def test_structured_side_effect_row():
    row = format_side_effect_row(_packet()["side_effects"][0])
    assert "uncodable" in row
    assert "MAX_EMAIL_LENGTH" in row
    assert "no successor" in row.lower() or "successor" in row


def test_checklist_includes_leftover_and_side_effects():
    packet = _packet()
    leftovers = [
        _L(
            "src/each_item.py:13  @field_validator each_item=True  "
            "(decorated_def_options: v2 has no each_item)"
        )
    ]
    text = format_human_checklist(packet, leftovers=leftovers)
    assert "Human checks" in text
    assert "each_item" in text
    assert "MAX_EMAIL_LENGTH" in text
    assert "multi_step" in text
    md = format_human_checklist_markdown(packet, leftovers=leftovers)
    assert "Double-check" in md
    assert "each_item" in md


def test_run_summary_human_checklist_section():
    packet = _packet()
    leftover = _L("src/x.py:1  each_item=True")
    summary = build_run_summary(
        packet=packet,
        report=PatchReport(),
        test_result=TestResult(
            runner="t",
            passed=False,
            returncode=1,
            stdout="",
            stderr="",
            command=[],
        ),
        leftover_items=[leftover],
    )
    text = format_run_summary(summary)
    assert "Human checks (copy into PR)" in text
    assert "MAX_EMAIL_LENGTH" in text
    assert "leftover: src/x.py:1" in text


def test_pr_body_double_check_section():
    packet = _packet()
    body = build_pr_body(
        packet,
        PatchReport(),
        TestResult(
            runner="t",
            passed=True,
            returncode=0,
            stdout="",
            stderr="",
            command=[],
        ),
        leftovers=[_L("src/each_item.py:13  each_item=True")],
    )
    assert "Double-check (human)" in body
    assert "MAX_EMAIL_LENGTH" in body
    assert "each_item" in body


def test_empty_checklist_when_no_gaps():
    packet = {
        "packet_id": "x",
        "package": "x",
        "ecosystem": "pypi",
        "from_version": "1",
        "to_version": "2",
        "rules": [],
        "side_effects": [],
    }
    assert build_human_checklist(packet) == []
    assert format_human_checklist(packet) == ""
