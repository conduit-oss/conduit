"""Validate surface packets against the public surface schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore


def surface_schema_path() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[4] / "schema" / "conduit-surface-packet.schema.json",
        here.parents[3] / "schema" / "conduit-surface-packet.schema.json",
        here.parents[1] / "schema" / "conduit-surface-packet.schema.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("conduit-surface-packet.schema.json not found")


def load_surface_schema() -> dict[str, Any]:
    return json.loads(surface_schema_path().read_text(encoding="utf-8"))


def validate_surface_packet(packet: dict[str, Any]) -> list[str]:
    """Return validation error messages (empty if valid)."""
    if jsonschema is None:
        required = [
            "packet_kind",
            "packet_id",
            "package",
            "ecosystem",
            "version",
            "source",
            "symbols",
            "checksum",
        ]
        return [f"missing {k}" for k in required if k not in packet]
    schema = load_surface_schema()
    validator = jsonschema.Draft202012Validator(schema)
    return sorted(
        f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
        for err in validator.iter_errors(packet)
    )
