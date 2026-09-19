"""Offline pydantic validator-hop smoke: Watch dirty then clean without LLM keys."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conduit.packet.validate import validate_packet
from conduit.patcher.engine import apply_packet
from conduit.prune.grep_imports import prune_by_imports
from conduit.watch import evaluate_watch

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-validator-fixture"
PACKET_PATH = REPO / "examples" / "sample-packet" / "pydantic-validator-hop.json"


def _packet() -> dict:
    return json.loads(PACKET_PATH.read_text(encoding="utf-8"))


def _copy_fixture(dest: Path) -> Path:
    shutil.copytree(
        FIXTURE,
        dest,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", ".conduit"),
    )
    return dest


def _residue_counts(root: Path) -> tuple[int, int]:
    text = (root / "src" / "model.py").read_text(encoding="utf-8")
    return text.count(".dict("), text.count("class Config")


def test_pydantic_validator_hop_packet_validates():
    data = _packet()
    assert validate_packet(data) == []
    types = {r.get("type") for r in data["rules"]}
    assert "DEPENDENCY_BUMP" in types
    assert "AST_IMPORT_REWRITE" in types
    assert "AST_CALL_REWRITE" in types
    effects = data.get("side_effects") or []
    joined = " ".join(str(e.get("detail") or "") for e in effects)
    assert ".dict()" in joined
    assert "Config" in joined
    assert "classmethod" in joined.lower() or "signature" in joined.lower()


def test_pydantic_validator_smoke_watch_apply_idempotent(tmp_path: Path):
    tree = _copy_fixture(tmp_path / "fixture")
    packet = _packet()
    src = tree / "src" / "model.py"

    dirty_tree = _copy_fixture(tmp_path / "dirty")
    (dirty_tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    dirty = evaluate_watch(root=dirty_tree, packet=packet)
    assert dirty.status == "bump_dirty"
    assert dirty.exit_code != 0
    assert any(item.callee == "validator" for item in dirty.leftovers)

    files = prune_by_imports(tree, ["pydantic"])
    report = apply_packet(tree, packet, dry_run=False, file_allowlist=files or None)
    assert report.files_modified
    after = src.read_text(encoding="utf-8")
    assert '@field_validator("name")' in after
    assert "field_field_validator" not in after
    assert "from pydantic import BaseModel, field_validator" in after
    assert "pydantic==2.0.0" in (tree / "requirements.txt").read_text(encoding="utf-8")

    before_second = {
        "model": src.read_text(encoding="utf-8"),
        "req": (tree / "requirements.txt").read_text(encoding="utf-8"),
    }
    apply_packet(tree, packet, dry_run=False, file_allowlist=files or None)
    assert src.read_text(encoding="utf-8") == before_second["model"]
    assert (tree / "requirements.txt").read_text(encoding="utf-8") == before_second["req"]
    assert "field_field_validator" not in src.read_text(encoding="utf-8")

    clean = evaluate_watch(root=tree, packet=packet)
    assert clean.status == "clean"
    assert clean.exit_code == 0
    assert clean.leftovers == ()
    assert not any(item.callee == "validator" for item in clean.leftovers)

    dict_hits, config_hits = _residue_counts(tree)
    assert dict_hits >= 1
    assert config_hits >= 1
