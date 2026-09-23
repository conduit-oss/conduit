"""Receipt-validate the catalog-facing pydantic surface hop freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conduit.packet.validate import validate_packet

REPO = Path(__file__).resolve().parents[2]
HOP = REPO / "examples" / "sample-packet" / "pydantic-surface-hop.json"
DRAFT_ALIAS = REPO / "examples" / "sample-packet" / "pydantic-surface-diff-draft.json"
EXPECTED_SHA256 = "86fb8cf06c8a7af2eb159c634fae54e816c52677d6c6fb367ce2b9c50d747c94"


def test_pydantic_surface_hop_freeze_validates_and_matches_sha():
    assert HOP.is_file(), HOP
    text = HOP.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == EXPECTED_SHA256
    data = json.loads(text)
    assert validate_packet(data) == []
    assert data["packet_id"] == "pydantic-1.10.13-2.0.0"
    assert data["from_version"] == "1.10.13"
    assert data["to_version"] == "2.0.0"
    types = {r.get("type") for r in data["rules"]}
    assert "DEPENDENCY_BUMP" in types
    assert "AST_CALL_REWRITE" in types
    assert "AST_DECLARATION_REWRITE" in types
    ops = [
        r.get("operation") or {}
        for r in data["rules"]
        if r.get("type") == "AST_DECLARATION_REWRITE"
    ]
    detector = [op for op in ops if op.get("kind") == "decorated_def_convention"]
    assert {op.get("decorator") for op in detector} >= {
        "field_validator",
        "model_validator",
    }
    assert all(op.get("unknown_params") == "refuse" for op in detector)
    assert all(op.get("unknown_options") == "refuse" for op in detector)
    effects = [e for e in (data.get("side_effects") or []) if isinstance(e, dict)]
    overclaim = "no signature/mode declaration op yet"
    assert not any(
        overclaim in str(e.get("blocker") or "")
        or e.get("old_shape") == "def check_name(cls, v)"
        for e in effects
    )
    assert any(
        e.get("gap_kind") == "multi_step"
        and "mode=" in str(e.get("new_shape") or "")
        for e in effects
    )


def test_pydantic_surface_diff_draft_aliases_hop():
    hop = HOP.read_text(encoding="utf-8").replace("\r\n", "\n")
    draft = DRAFT_ALIAS.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert hop == draft
