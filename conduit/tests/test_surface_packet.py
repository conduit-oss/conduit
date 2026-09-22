"""Surface packet mint + schema tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.packet.surface_mint import (
    checksum_surface_packet,
    mint_surface_packet_from_tree,
    write_surface_packet,
)
from conduit.packet.surface_validate import validate_surface_packet


def _toy_tree(root: Path) -> Path:
    pkg = root / "toykit"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        '"""Toy public surface."""\n'
        "__all__ = ['Public', 'helper']\n"
        "\n"
        "class Public:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "def helper():\n"
        "    return 2\n"
        "\n"
        "def _private():\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return root


def test_mint_surface_from_tree_valid_and_stable(tmp_path: Path):
    tree = _toy_tree(tmp_path / "src")
    once = mint_surface_packet_from_tree(
        tree,
        package="toykit",
        version="1.0.0",
        ecosystem="pypi",
        source_kind="tree",
        locator=str(tree),
    )
    assert validate_surface_packet(once) == []
    assert once["packet_kind"] == "surface"
    assert once["checksum"] == checksum_surface_packet(once)
    ids = {s["id"] for s in once["symbols"]}
    assert "Public" in ids
    assert "helper" in ids
    assert "Public.run" in ids
    assert not any(i.startswith("_") for i in ids)

    twice = mint_surface_packet_from_tree(
        tree,
        package="toykit",
        version="1.0.0",
        ecosystem="pypi",
        source_kind="tree",
        locator=str(tree),
    )
    assert twice["checksum"] == once["checksum"]
    assert [s["id"] for s in twice["symbols"]] == [s["id"] for s in once["symbols"]]


def test_cli_packet_snapshot_from_tree(tmp_path: Path):
    tree = _toy_tree(tmp_path / "src")
    out = tmp_path / "toykit-pypi-1.0.0.json"
    result = CliRunner().invoke(
        app,
        [
            "packet",
            "snapshot",
            "--package",
            "toykit",
            "--version",
            "1.0.0",
            "--from-tree",
            str(tree),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert validate_surface_packet(data) == []
    assert data["package"] == "toykit"
    write_surface_packet(data, tmp_path / "copy.json")
