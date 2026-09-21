#!/usr/bin/env python3
"""Check a remint freeze receipt (surface families). Opt-in hop gold shapes."""

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
        help="Deprecated: ignored; receipt is surface-family based",
    )
    parser.add_argument(
        "--hop-gold",
        action="store_true",
        help="Also require dict/validator/Config mechanical shapes (hop proof)",
    )
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    tokens = tuple(args.tokens) if args.tokens else None
    receipt = build_remint_receipt(packet, fixture_tokens=tokens)
    if args.hop_gold:
        from conduit.packet.obligations import evaluate_gold_obligations

        gold = evaluate_gold_obligations(packet, config_manual_allowed=False)
        receipt = dict(receipt)
        receipt["obligations"] = gold["obligations"]
        receipt["ok"] = bool(receipt["ok"]) and bool(gold["ok"])
    print(json.dumps(receipt, indent=2, sort_keys=True))
    try:
        assert_remint_receipt_ok(packet, fixture_tokens=tokens)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.hop_gold and not receipt["ok"]:
        print(
            f"hop gold failed: obligations={receipt.get('obligations')}",
            file=sys.stderr,
        )
        return 1
    print("remint receipt ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
