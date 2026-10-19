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
# Kept outside impact/wheels so evidence classifier vendor/API scans stay stable.
APPLY_WHEELS = REPO / "conduit" / "fixtures" / "apply" / "wheels"
OLD = WHEELS / "SQLAlchemy-1.4.52-cp310-cp310-macosx_11_0_x86_64.whl"
NEW = WHEELS / "sqlalchemy-2.1.4-py3-none-any.whl"
OAI_OLD = APPLY_WHEELS / "openai-0.28.1-py3-none-any.whl"
OAI_NEW = APPLY_WHEELS / "openai-1.51.0-py3-none-any.whl"


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


@pytest.mark.timeout(180)
def test_openai_chat_completions_create_in_top_candidates():
    """Recorded openai 0.28.1 -> 1.51.0: Completions.create beats TypedDict noise."""
    assert OAI_OLD.is_file() and OAI_NEW.is_file()
    diff = diff_wheels(OAI_OLD, OAI_NEW)
    target = "openai.resources.chat.completions.Completions.create"
    assert target in diff.added_signatures
    cands = pick_candidates(
        "openai.ChatCompletion.create",
        diff.added_signatures,
        added_params=diff.added_params,
        old_params=diff.before_params.get("openai.ChatCompletion.create"),
        call_kwargs=("model", "messages"),
    )
    assert cands
    top = [row["symbol"] for row in cands[:3]]
    assert any(
        name.endswith("Completions.create") and "chat" in name.lower() for name in top
    ), top


def test_param_overlap_ranks_rename_over_token_noise(tmp_path: Path):
    """fetch(id, timeout) -> retrieve(id, timeout) wins over fetch_config noise."""
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    for root in (old_root, new_root):
        pkg = root / "acme"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("from .client import Client\n", encoding="utf-8")
    (old_root / "acme" / "client.py").write_text(
        "class Client:\n"
        "    def fetch(self, id, timeout=None):\n"
        "        return id\n",
        encoding="utf-8",
    )
    (new_root / "acme" / "client.py").write_text(
        "class Client:\n"
        "    def retrieve(self, id, timeout=None):\n"
        "        return id\n"
        "    def fetch_config(self):\n"
        "        return {}\n"
        "\n"
        "class FetchConfig:\n"
        "    pass\n",
        encoding="utf-8",
    )
    diff = diff_wheels(old_root, new_root)
    removed = "acme.client.Client.fetch"
    assert removed not in {item.symbol for item in diff if item.kind == "added"}
    assert any(name.endswith("Client.retrieve") for name in diff.added_signatures)
    cands = pick_candidates(
        removed,
        diff.added_signatures,
        added_params=diff.added_params,
        old_params=diff.before_params.get(removed) or ("id", "timeout"),
        call_kwargs=("id", "timeout"),
    )
    assert cands
    assert cands[0]["symbol"].endswith("Client.retrieve"), [row["symbol"] for row in cands]


def test_plural_fold_and_leaf_prefer_callable_params():
    added = {
        "lib.types.chat.completion_create_params.CompletionCreateParamsBase": (
            "lib.types.chat.completion_create_params.CompletionCreateParamsBase()"
        ),
        "lib.resources.chat.completions.Completions.create": (
            "lib.resources.chat.completions.Completions.create(*, messages, model)"
        ),
    }
    added_params = {
        "lib.types.chat.completion_create_params.CompletionCreateParamsBase": (),
        "lib.resources.chat.completions.Completions.create": ("messages", "model"),
    }
    cands = pick_candidates(
        "lib.ChatCompletion.create",
        added,
        added_params=added_params,
        call_kwargs=("messages", "model"),
    )
    assert cands[0]["symbol"].endswith("Completions.create")
