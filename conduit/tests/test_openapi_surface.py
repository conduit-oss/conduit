"""OpenAPI → surface packet mint + diff."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.packet.openapi_surface import mint_surface_from_openapi
from conduit.packet.surface_diff import diff_surface_packets
from conduit.packet.surface_mint import checksum_surface_packet, write_surface_packet
from conduit.packet.surface_validate import validate_surface_packet
from conduit.packet.validate import validate_packet

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "examples" / "openapi-fixture"


def test_mint_surface_from_openapi_valid_and_stable():
    path = FIXTURE_DIR / "previous.openapi.yaml"
    once = mint_surface_from_openapi(
        path,
        package="demo-api",
        version="1.0.0",
        ecosystem="other",
    )
    assert validate_surface_packet(once) == []
    assert once["packet_kind"] == "surface"
    assert once["source"]["kind"] == "tree"
    assert once["checksum"] == checksum_surface_packet(once)
    ids = {s["id"] for s in once["symbols"]}
    assert ids == {
        "GET /v1/models",
        "POST /v1/completions",
        "DELETE /v1/engines/{engine_id}",
    }
    assert all(s["kind"] == "export" for s in once["symbols"])

    twice = mint_surface_from_openapi(
        path,
        package="demo-api",
        version="1.0.0",
        ecosystem="other",
    )
    assert twice["checksum"] == once["checksum"]
    assert [s["id"] for s in twice["symbols"]] == [s["id"] for s in once["symbols"]]


def test_diff_openapi_surfaces_emits_call_and_side_effects():
    prev = mint_surface_from_openapi(
        FIXTURE_DIR / "previous.openapi.yaml",
        package="demo-api",
        version="1.0.0",
        locator="examples/openapi-fixture/previous.openapi.yaml",
    )
    latest = mint_surface_from_openapi(
        FIXTURE_DIR / "latest.openapi.yaml",
        package="demo-api",
        version="2.0.0",
        locator="examples/openapi-fixture/latest.openapi.yaml",
    )
    hop = diff_surface_packets(prev, latest)
    assert validate_packet(hop) == []

    callees = {
        (r.get("old_callee"), r.get("new_callee"))
        for r in hop["rules"]
        if r.get("type") == "AST_CALL_REWRITE"
    }
    assert ("POST /v1/completions", "POST /v1/chat/completions") in callees

    removed_shapes = {e.get("old_shape") for e in (hop.get("side_effects") or [])}
    assert "DELETE /v1/engines/{engine_id}" in removed_shapes



def test_cli_packet_snapshot_openapi(tmp_path: Path):
    out = tmp_path / "demo-api-other-1.0.0.json"
    result = CliRunner().invoke(
        app,
        [
            "packet",
            "snapshot",
            "--package",
            "demo-api",
            "--version",
            "1.0.0",
            "--openapi",
            str(FIXTURE_DIR / "previous.openapi.yaml"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert validate_surface_packet(data) == []
    assert len(data["symbols"]) == 3
    write_surface_packet(data, tmp_path / "copy.json")
