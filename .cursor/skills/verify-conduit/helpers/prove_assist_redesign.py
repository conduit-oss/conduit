#!/usr/bin/env python3
"""Prove opt-in redesign assist on each_item with a deterministic stub proposer."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_repo() -> Path:
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "conduit" / "src" / "conduit").is_dir():
            return cand
    raise SystemExit("repo root not found")


def main() -> int:
    repo = find_repo()
    src = str(repo / "conduit" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)

    from conduit.patcher.leftovers import scan_packet_leftovers
    from conduit.patcher.redesign_assist import (
        collect_redesign_leftovers,
        deterministic_drop_unmapped_kwargs,
        run_redesign_assist,
    )

    skill = repo / ".cursor" / "skills" / "verify-conduit"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = skill / "evidence" / f"assist-redesign-{run_id}"
    scratch = skill / "scratch" / f"assist-{run_id}"
    evidence.mkdir(parents=True, exist_ok=True)
    if scratch.exists():
        shutil.rmtree(scratch)
    (scratch / "src").mkdir(parents=True)
    shutil.copy(
        repo / "examples" / "pydantic-convention-fixture" / "src" / "each_item.py",
        scratch / "src" / "each_item.py",
    )
    (scratch / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    packet = json.loads(
        (repo / "examples" / "sample-packet" / "pydantic-surface-hop.json").read_text(
            encoding="utf-8"
        )
    )
    before = scan_packet_leftovers(scratch, packet)
    (evidence / "leftovers.before.json").write_text(
        json.dumps([x.display() for x in before], indent=2) + "\n", encoding="utf-8"
    )
    (evidence / "each_item.before.py").write_text(
        (scratch / "src" / "each_item.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = run_redesign_assist(
        scratch, packet, before, propose=deterministic_drop_unmapped_kwargs
    )
    after_text = (scratch / "src" / "each_item.py").read_text(encoding="utf-8")
    (evidence / "each_item.after.py").write_text(after_text, encoding="utf-8")
    (evidence / "assist.message.txt").write_text(result.message + "\n", encoding="utf-8")
    remaining = collect_redesign_leftovers(result.leftovers_after)
    (evidence / "leftovers.after.json").write_text(
        json.dumps([x.display() for x in result.leftovers_after], indent=2) + "\n",
        encoding="utf-8",
    )
    ok = (
        result.attempted
        and bool(result.applied)
        and "each_item=True" not in after_text
        and not remaining
    )
    (evidence / "RESULT.txt").write_text("PASS\n" if ok else "FAIL\n", encoding="utf-8")
    print(f"evidence={evidence}")
    print(result.message)
    print("RESULT=PASS" if ok else "RESULT=FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
