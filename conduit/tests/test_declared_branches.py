"""A value that is one of several branches (``a or b``, ``a and b``, ``a if c else b``,
``(w := a)``) is checked branch by branch, the way a plain value is.

Made-up libraries only: the parcel SDK and shop repo from ``test_declared_types``.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from conduit.apply.nonecheck import none_breaks

from test_declared_types import _findings


@pytest.mark.parametrize(
    "body, value",
    [
        ("Record(weight=reply.weight or Weight(1))", "reply.weight"),
        ("Record(weight=Weight.from_kilos(1.0) or reply.weight)", "reply.weight"),
        ("Record(weight=reply.weight if flag else Weight(1))", "reply.weight"),
        ("Record(weight=Weight(1) if flag else reply.weight)", "reply.weight"),
        ("Record(weight=reply.label and reply.weight)", "reply.weight"),
        ("Record(weight=(picked := reply.weight))", "reply.weight"),
        ("Record(weight=(reply.spare or (reply.weight if flag else Weight(1))))", "reply.spare"),
        ("Record(weight=reply.weight or unknown_default())", "reply.weight"),
    ],
)
def test_a_release_class_in_any_branch_is_a_mismatch(tmp_path: Path, body: str, value: str):
    found = _findings(tmp_path, f"def build(reply: Reply, flag: bool) -> None:\n    {body}\n")
    assert found, body
    assert f"weight= passes parcel_sdk.types.weight.Weight (from {value})" in found[0], found
    assert "Record in this repo expects shop.records.Weight" in found[0]


def test_a_name_bound_to_a_branching_value_is_followed(tmp_path: Path):
    found = _findings(
        tmp_path,
        """
        def build(reply: Reply, flag: bool) -> None:
            picked = reply.weight if flag else Weight(1)
            Record(weight=picked)
        """,
    )
    assert len(found) == 1 and "(from reply.weight)" in found[0], found


def test_branches_that_all_fit_or_cannot_be_read_are_not_reported(tmp_path: Path):
    assert _findings(
        tmp_path,
        """
        def build(reply: Reply, holder: Holder, flag: bool, other) -> None:
            Record(weight=Weight(1) or Weight.from_kilos(reply.weight.kilos))
            Record(weight=holder.heavy if flag else Weight(2))
            Record(weight=other or Weight(1))
            Record(weight=other.weight if flag else None)
            Either(weight=reply.weight or Weight(1))
            Maybe(weight=Weight(1) or None)
        """,
    ) == []


def test_a_branch_the_original_code_already_passed_is_not_the_rewrites(tmp_path: Path):
    body = "def build(reply: Reply, flag: bool) -> None:\n    Record(weight=reply.weight or Weight(1))\n"
    original = (
        "from parcel_sdk.types import Reply\nfrom shop.records import Record, Weight\n\n"
        "def build(reply: Reply, flag: bool) -> None:\n    Record(weight=reply.weight)\n"
    )
    assert _findings(tmp_path, body, original=original) == []


_RELEASE = textwrap.dedent(
    """\
    from typing import Optional

    class Reading:
        kilos: float
        spare_kilos: Optional[float] = None
    """
)


def _none_found(tmp_path: Path, body: str) -> list[str]:
    modules = {"parcel_sdk.types": ast.parse(_RELEASE)}
    klass = next(node for node in modules["parcel_sdk.types"].body if isinstance(node, ast.ClassDef))
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "scale.py").write_text(
        "class Scale:\n    def __init__(self, kilos: float, spare: float):\n        self.total = kilos + spare\n",
        encoding="utf-8",
    )
    source = "from scale import Scale\n\n" + textwrap.dedent(body)
    (root / "mod.py").write_text(source, encoding="utf-8")
    tree = ast.parse(source)
    func = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    return none_breaks(modules, root, {"mod.py": tree}, "mod.py", tree, func, {"r": [("parcel_sdk.types", klass)]}, "parcel-sdk", "2.0")


def test_or_zero_still_guards_none_and_is_not_a_class_mismatch(tmp_path: Path):
    """None-ness and class identity are separate checks: `x or 0` passes the None
    check, and a literal default is not a class the declared-type check reads."""
    assert _none_found(tmp_path, "def f(r):\n    return Scale(r.kilos, r.spare_kilos or 0)\n") == []
    assert _none_found(tmp_path, "def f(r):\n    return Scale(r.kilos, r.spare_kilos if r.spare_kilos is not None else 0)\n") == []
    assert _none_found(tmp_path, "def f(r):\n    return Scale(r.kilos, r.spare_kilos)\n")
    assert _findings(tmp_path / "typed", "def build(reply: Reply) -> None:\n    keep(reply.spare or 0)\n") != []
    assert _findings(tmp_path / "plain", "def build(reply: Reply, n: int) -> None:\n    Maybe(weight=n or 0)\n") == []
