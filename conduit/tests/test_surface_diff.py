"""Surface packet diff → migration draft."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.packet.surface_diff import diff_surface_packets
from conduit.packet.surface_mint import mint_surface_packet_from_tree, write_surface_packet
from conduit.packet.surface_validate import validate_surface_packet
from conduit.packet.validate import validate_packet
from conduit.patcher.engine import apply_packet


def _surface(package: str, version: str, ids: list[str]) -> dict:
    return {
        "packet_kind": "surface",
        "packet_id": f"surface:pypi:{package}:{version}",
        "package": package,
        "ecosystem": "pypi",
        "version": version,
        "source": {"kind": "wheel", "locator": f"{package}=={version}"},
        "symbols": [{"id": i, "kind": "export"} for i in ids],
        "checksum": "0",
    }


def test_supersession_when_legacy_and_successor_on_new():
    s1 = _surface(
        "demo",
        "1.0.0",
        ["BaseModel.dict", "validator"],
    )
    s2 = _surface(
        "demo",
        "2.0.0",
        [
            "BaseModel.dict",
            "BaseModel.model_dump",
            "validator",
            "field_validator",
        ],
    )
    hop = diff_surface_packets(s1, s2)
    assert validate_packet(hop) == []
    callees = {
        (r.get("old_callee"), r.get("new_callee"))
        for r in hop["rules"]
        if r.get("type") == "AST_CALL_REWRITE"
    }
    assert ("BaseModel.dict", "BaseModel.model_dump") in callees
    assert ("validator", "field_validator") in callees


def _write_toy_v1(root: Path) -> Path:
    pkg = root / "toykit"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "def run():\n"
        "    return 1\n"
        "\n"
        "def helper():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    return root


def _write_toy_v2(root: Path) -> Path:
    pkg = root / "toykit"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "def execute():\n"
        "    return 1\n"
        "\n"
        "def helper():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    return root


def test_diff_surface_maps_rename_and_apply(tmp_path: Path):
    v1_tree = _write_toy_v1(tmp_path / "v1")
    v2_tree = _write_toy_v2(tmp_path / "v2")
    s1 = mint_surface_packet_from_tree(
        v1_tree, package="toykit", version="1.0.0", source_kind="tree"
    )
    s2 = mint_surface_packet_from_tree(
        v2_tree, package="toykit", version="2.0.0", source_kind="tree"
    )
    # Force a confident leaf rename the export heuristic can see.
    hop = diff_surface_packets(s1, s2)
    assert validate_packet(hop) == []
    assert any(r.get("type") == "DEPENDENCY_BUMP" for r in hop["rules"])
    call = [
        r
        for r in hop["rules"]
        if r.get("type") == "AST_CALL_REWRITE"
        and r.get("old_callee") == "run"
        and r.get("new_callee") == "execute"
    ]
    assert call, hop["rules"]

    consumer = tmp_path / "app"
    consumer.mkdir()
    (consumer / "requirements.txt").write_text("toykit==1.0.0\n", encoding="utf-8")
    (consumer / "main.py").write_text(
        "from toykit import run\n\n"
        "def go():\n"
        "    return run()\n",
        encoding="utf-8",
    )
    apply_packet(consumer, hop, require_context=False)
    text = (consumer / "main.py").read_text(encoding="utf-8")
    assert "execute()" in text
    assert "import execute" in text or "from toykit import execute" in text

def test_cli_diff_surface(tmp_path: Path):
    v1_tree = _write_toy_v1(tmp_path / "v1")
    v2_tree = _write_toy_v2(tmp_path / "v2")
    s1 = mint_surface_packet_from_tree(
        v1_tree, package="toykit", version="1.0.0", source_kind="tree"
    )
    s2 = mint_surface_packet_from_tree(
        v2_tree, package="toykit", version="2.0.0", source_kind="tree"
    )
    p1 = write_surface_packet(s1, tmp_path / "s1.json")
    p2 = write_surface_packet(s2, tmp_path / "s2.json")
    out = tmp_path / "hop.json"
    result = CliRunner().invoke(
        app,
        [
            "packet",
            "diff-surface",
            "--from",
            str(p1),
            "--to",
            str(p2),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    hop = json.loads(out.read_text(encoding="utf-8"))
    assert validate_packet(hop) == []
