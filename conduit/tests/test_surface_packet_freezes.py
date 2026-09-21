"""Offline checks for frozen pydantic surface packets."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.packet.surface_validate import validate_surface_packet

REPO = Path(__file__).resolve().parents[2]
SURFACES = REPO / "examples" / "surface-packets"


def test_pydantic_surface_freezes_validate():
    for name in ("pydantic-pypi-1.10.13.json", "pydantic-pypi-2.0.0.json"):
        path = SURFACES / name
        assert path.is_file(), path
        data = json.loads(path.read_text(encoding="utf-8"))
        assert validate_surface_packet(data) == []
        assert data["packet_kind"] == "surface"
        assert len(data["symbols"]) > 100
        assert not any(s["id"].startswith("_") for s in data["symbols"])
