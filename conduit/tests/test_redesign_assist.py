"""Opt-in redesign assist for multi_step declaration leftovers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conduit.patcher.leftovers import Leftover, scan_packet_leftovers
from conduit.patcher.redesign_assist import (
    collect_redesign_leftovers,
    deterministic_drop_unmapped_kwargs,
    is_redesign_leftover,
    run_redesign_assist,
)

REPO = Path(__file__).resolve().parents[2]
HOP = REPO / "examples" / "sample-packet" / "pydantic-surface-hop.json"
EACH = REPO / "examples" / "pydantic-convention-fixture" / "src" / "each_item.py"


def _packet() -> dict:
    return json.loads(HOP.read_text(encoding="utf-8"))


def test_is_redesign_leftover_filters_uncodable():
    assert is_redesign_leftover(
        Leftover(
            rel="a.py",
            callee="@field_validator each_item=True",
            reason="decorated_def_options: v2 has no each_item",
        )
    )
    assert not is_redesign_leftover(
        Leftover(
            rel="a.py",
            callee="MAX_EMAIL_LENGTH",
            reason="uncodable: no rename match",
        )
    )


def test_assist_clears_each_item_with_stub_proposer(tmp_path: Path):
    tree = tmp_path / "consumer"
    (tree / "src").mkdir(parents=True)
    shutil.copy(EACH, tree / "src" / "each_item.py")
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    packet = _packet()
    before = scan_packet_leftovers(tree, packet)
    redesign = collect_redesign_leftovers(before)
    assert redesign, f"expected redesign leftovers, got {before}"

    result = run_redesign_assist(
        tree, packet, before, propose=deterministic_drop_unmapped_kwargs
    )
    assert result.attempted
    assert result.applied
    text = (tree / "src" / "each_item.py").read_text(encoding="utf-8")
    assert "each_item=True" not in text
    assert not collect_redesign_leftovers(result.leftovers_after)


def test_assist_nonsense_propose_fails_closed(tmp_path: Path):
    tree = tmp_path / "consumer"
    (tree / "src").mkdir(parents=True)
    shutil.copy(EACH, tree / "src" / "each_item.py")
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    packet = _packet()
    before = scan_packet_leftovers(tree, packet)

    def bad_propose(*, root, leftover, source):  # noqa: ANN001
        # Leave each_item in place — pretend LLM "helped"
        return source

    result = run_redesign_assist(tree, packet, before, propose=bad_propose)
    assert result.attempted
    assert collect_redesign_leftovers(result.leftovers_after)
    assert "each_item=True" in (tree / "src" / "each_item.py").read_text(
        encoding="utf-8"
    )
