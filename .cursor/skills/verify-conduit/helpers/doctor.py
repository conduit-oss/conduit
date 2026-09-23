#!/usr/bin/env python3
"""Read-only doctor: is this Conduit checkout worth driving for consumer migration?"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def find_repo() -> Path:
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "conduit" / "src" / "conduit").is_dir():
            return cand
    raise SystemExit("could not locate repo root containing conduit/src/conduit")


def main() -> int:
    repo = find_repo()
    errors: list[str] = []
    print(f"repo={repo}")
    print(f"python={sys.version.split()[0]}")

    src_root = str(repo / "conduit" / "src")
    if src_root not in sys.path:
        sys.path.insert(0, src_root)

    try:
        import conduit  # noqa: F401
        from conduit.main import app  # noqa: F401

        print("conduit_import=ok")
        print(f"conduit_file={Path(conduit.__file__).resolve()}")
    except Exception as exc:
        errors.append(f"conduit import failed: {exc}")
        print("conduit_import=FAIL")

    packets = (
        repo / "examples" / "sample-packet" / "pydantic-surface-hop.json",
        repo / "examples" / "sample-packet" / "pydantic-validator-hop.json",
        repo / "examples" / "sample-packet" / "conduit-packet.json",
        repo / "examples" / "reshape-recipes" / "pydantic-1.10.13-2.0.0.json",
    )
    trees = (
        repo / "examples" / "pydantic-validator-fixture",
        repo / "examples" / "pydantic-convention-fixture",
        repo / "examples" / "demo-consumer",
    )

    for path in packets:
        rel = path.relative_to(repo).as_posix()
        if not path.is_file():
            errors.append(f"missing packet: {rel}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"packet_ok={rel} id={data.get('packet_id') or data.get('package')}")
        except Exception as exc:
            errors.append(f"unreadable packet {rel}: {exc}")

    for path in trees:
        rel = path.relative_to(repo).as_posix()
        if not path.is_dir():
            errors.append(f"missing consumer tree: {rel}")
        else:
            print(f"tree_ok={rel}")

    if errors:
        print("doctor=FAIL")
        for e in errors:
            print(f"error: {e}")
        return 1
    print("doctor=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
