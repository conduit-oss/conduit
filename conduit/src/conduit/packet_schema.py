"""Validate a hop document against the public packet schema.

Feed watch and map blast radius still read these documents. Authoring and
rewriting them is not part of this package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore


def schema_path() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "schema" / "conduit-packet.schema.json",
        here.parents[2] / "schema" / "conduit-packet.schema.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("conduit-packet.schema.json not found")


def load_schema() -> dict[str, Any]:
    return json.loads(schema_path().read_text(encoding="utf-8"))


def validate_packet(packet: dict[str, Any]) -> list[str]:
    """Return a list of validation error messages (empty if valid)."""
    if jsonschema is None:
        required = ["packet_id", "package", "ecosystem", "from_version", "to_version", "rules"]
        return [f"missing {k}" for k in required if k not in packet]
    schema = load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    return sorted(
        f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
        for err in validator.iter_errors(packet)
    )
