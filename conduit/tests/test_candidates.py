"""Replacement candidates from recorded wheel diffs (no vendor tables)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conduit.impact.api import diff_wheels
from conduit.impact.candidates import (
    MAX_CANDIDATES,
    needs_replacement_candidates,
    pick_candidates,
    symbol_tokens,
)

REPO = Path(__file__).resolve().parents[2]
WHEELS = REPO / "conduit" / "fixtures" / "impact" / "wheels"
OLD = WHEELS / "SQLAlchemy-1.4.52-cp310-cp310-macosx_11_0_x86_64.whl"
NEW = WHEELS / "sqlalchemy-2.1.4-py3-none-any.whl"


def test_pick_candidates_shares_tokens_and_caps():
    added = {
        "lib.chat.completions.Completions.create": "lib.chat.completions.Completions.create(self, *, messages)",
        "lib.unrelated.helper": "lib.unrelated.helper()",
        "lib.chat.Completions.create": "lib.chat.Completions.create(self)",
        "lib.embeddings.create": "lib.embeddings.create(self, input)",
    }
    # Pad with noise that shares nothing.
    for i in range(20):
        added[f"lib.noise.fn{i}"] = f"lib.noise.fn{i}()"
    cands = pick_candidates("lib.ChatCompletion.create", added)
    assert 1 <= len(cands) <= MAX_CANDIDATES
    symbols = [row["symbol"] for row in cands]
    assert any("create" in name and "chat" in name.lower() for name in symbols)
    assert all("noise" not in row["symbol"] for row in cands)
    assert all("signature" in row and row["signature"] for row in cands)
    # Stronger token overlap ranks first.
    assert "chat" in symbols[0].lower() and "create" in symbols[0]


def test_needs_replacement_candidates_only_when_new_missing():
    assert needs_replacement_candidates(kind="removed", new="")
    assert needs_replacement_candidates(kind="renamed", new="")
    assert not needs_replacement_candidates(kind="removed", new="replacement")
    assert not needs_replacement_candidates(kind="param_renamed", new="")
    assert not needs_replacement_candidates(kind="response_shape", new="model")


@pytest.mark.timeout(180)
def test_candidates_from_recorded_sqlalchemy_wheels():
    assert OLD.is_file() and NEW.is_file()
    diff = diff_wheels(OLD, NEW)
    assert getattr(diff, "added_signatures", None)
    removed = [item for item in diff if item.kind == "removed" and item.symbol]
    assert removed
    # Prefer a removal whose tokens overlap an added symbol (exercise the scorer).
    hit = None
    for item in removed:
        cands = pick_candidates(item.symbol, diff.added_signatures)
        if cands:
            hit = (item, cands)
            break
    assert hit is not None, "expected at least one removed symbol with token-sharing adds"
    item, cands = hit
    assert len(cands) <= MAX_CANDIDATES
    want = symbol_tokens(item.symbol)
    for row in cands:
        assert row["symbol"] in diff.added_signatures
        assert want & symbol_tokens(row["symbol"])
        assert row["signature"]
