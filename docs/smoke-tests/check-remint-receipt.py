#!/usr/bin/env python3
"""Check a remint freeze receipt (family counts + fixture tokens)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conduit" / "src"))

from conduit.packet.author import (  # noqa: E402
    assert_remint_receipt_ok,
    build_remint_receipt,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packet",
        type=Path,
        default=ROOT / "examples" / "sample-packet" / "pydantic-llm-mint-live.json",
    )
    parser.add_argument(
        "--token",
        action="append",
        dest="tokens",
        default=None,
        help="Fixture token that must appear in rules or side_effects (repeatable)",
    )
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    tokens = tuple(args.tokens) if args.tokens else None
    receipt = build_remint_receipt(packet, fixture_tokens=tokens)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    try:
        assert_remint_receipt_ok(packet, fixture_tokens=tokens)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("remint receipt ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
