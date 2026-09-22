"""Receipt-validate the catalog-facing pydantic surface hop freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conduit.packet.validate import validate_packet

REPO = Path(__file__).resolve().parents[2]
HOP = REPO / "examples" / "sample-packet" / "pydantic-surface-hop.json"
DRAFT_ALIAS = REPO / "examples" / "sample-packet" / "pydantic-surface-diff-draft.json"
EXPECTED_SHA256 = "c2fdb7a78a84042dc6af9ca9ac924631c78dbb0a1b41817887b0b801487271f5"


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
    assert any(
        isinstance(e, dict) and e.get("gap_kind") == "signature"
        for e in (data.get("side_effects") or [])
    )


def test_pydantic_surface_diff_draft_aliases_hop():
    hop = HOP.read_text(encoding="utf-8").replace("\r\n", "\n")
    draft = DRAFT_ALIAS.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert hop == draft
