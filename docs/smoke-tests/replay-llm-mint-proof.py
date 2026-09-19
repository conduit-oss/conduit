#!/usr/bin/env python3
"""Replay the P6 live LLM-mint proof: checksum, packet test, apply, Watch."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKET = ROOT / "examples" / "sample-packet" / "pydantic-llm-mint-live.json"
FIXTURE = ROOT / "examples" / "pydantic-validator-fixture"
EXPECTED_SHA256 = "e5ab11b54ff4927ccf9d3e818a2bf1a3bd31c8ba736250f7bf0b493025e32a8d"


def _conduit_bin() -> str:
    for name in ("conduit.exe", "conduit"):
        candidate = Path(sys.executable).with_name(name)
        if candidate.is_file():
            return str(candidate)
    return "conduit"


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd or ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def main() -> int:
    raw = PACKET.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SHA256:
        print(f"checksum mismatch: got {digest} want {EXPECTED_SHA256}", file=sys.stderr)
        return 1
    print(f"checksum ok {digest}")

    conduit = _conduit_bin()
    test = _run([conduit, "packet", "test", "--packet", str(PACKET)])
    print(test.stdout or test.stderr)
    if test.returncode != 0:
        return test.returncode

    with tempfile.TemporaryDirectory(prefix="conduit-p6-replay-") as tmp:
        tree = Path(tmp) / "consumer"
        shutil.copytree(FIXTURE, tree)
        t0 = time.perf_counter()
        apply = _run([conduit, "apply", "--path", str(tree), "--packet", str(PACKET)])
        watch = _run(
            [conduit, "watch", "--path", str(tree), "--packet", str(PACKET), "--json"]
        )
        elapsed = time.perf_counter() - t0
        apply_text = (apply.stdout or "") + (apply.stderr or "")
        watch_text = (watch.stdout or "") + (watch.stderr or "")
        print(apply_text)
        print(watch_text)
        if apply.returncode != 0:
            return apply.returncode
        if "packet enrichment" in apply_text.lower():
            print("unexpected packet enrich during apply", file=sys.stderr)
            return 1
        start = watch_text.find("{")
        end = watch_text.rfind("}")
        if start < 0 or end <= start:
            print("watch JSON missing", file=sys.stderr)
            return 1
        payload = json.loads(watch_text[start : end + 1])
        if payload.get("leftover_count") != 0:
            print(f"unexpected leftovers: {payload}", file=sys.stderr)
            return 1
        pin = (tree / "requirements.txt").read_text(encoding="utf-8")
        if "pydantic==2.0.0" not in pin:
            print(f"pin not bumped: {pin!r}", file=sys.stderr)
            return 1
        print(f"apply+watch {elapsed:.3f}s ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
