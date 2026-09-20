#!/usr/bin/env python3
"""Replay hand-enriched pydantic packet: apply then Watch on a fixture copy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_LLM_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CONDUIT_LLM_PROVIDER",
    "CONDUIT_LLM_API_KEY",
    "CONDUIT_LLM_BASE_URL",
)

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "examples" / "sample-packet" / "pydantic-llm-mint-live.json"
PACKET = (
    ROOT / "examples" / "sample-packet" / "pydantic-llm-mint-live-hand-enriched.json"
)
FIXTURE = ROOT / "examples" / "pydantic-validator-fixture"


def _conduit_bin() -> str:
    for name in ("conduit.exe", "conduit"):
        candidate = Path(sys.executable).with_name(name)
        if candidate.is_file():
            return str(candidate)
    return "conduit"


def _offline_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _LLM_ENV:
        env.pop(key, None)
    return env


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd or ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=_offline_env(),
    )


def main() -> int:
    live = json.loads(LIVE.read_text(encoding="utf-8"))
    if live.get("packet_id") != "pydantic-pypi-2.0.0":
        print(f"unexpected live packet_id: {live.get('packet_id')!r}", file=sys.stderr)
        return 1
    live_calls = {
        r.get("old_callee")
        for r in live.get("rules") or []
        if isinstance(r, dict) and r.get("type") == "AST_CALL_REWRITE"
    }
    if "validator" in live_calls:
        print("live mint was mutated with validator hop; use the sibling", file=sys.stderr)
        return 1
    live_digest = hashlib.sha256(LIVE.read_bytes()).hexdigest()
    print(f"live mint unmodified (no validator hop) sha256 {live_digest}")

    packet_digest = hashlib.sha256(PACKET.read_bytes()).hexdigest()
    print(f"hand-enriched sha256 {packet_digest}")

    conduit = _conduit_bin()
    test = _run([conduit, "packet", "test", "--packet", str(PACKET)])
    print(test.stdout or test.stderr)
    if test.returncode != 0:
        return test.returncode

    with tempfile.TemporaryDirectory(prefix="conduit-hand-enriched-") as tmp:
        tree = Path(tmp) / "consumer"
        shutil.copytree(FIXTURE, tree)
        src = tree / "src" / "model.py"
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
        if payload.get("status") != "clean" or payload.get("leftover_count") != 0:
            print(f"unexpected watch: {payload}", file=sys.stderr)
            return 1
        completeness = (payload.get("completeness") or {}).get("status")
        if completeness != "complete":
            print(f"completeness not complete: {payload.get('completeness')}", file=sys.stderr)
            return 1
        pin = (tree / "requirements.txt").read_text(encoding="utf-8")
        if "pydantic==2.0.0" not in pin:
            print(f"pin not bumped: {pin!r}", file=sys.stderr)
            return 1
        body = src.read_text(encoding="utf-8")
        checks = {
            "field_validator": '@field_validator("name")' in body,
            "no_double_prefix": "field_field_validator" not in body,
            "configdict_import": "field_validator, ConfigDict" in body,
            "no_class_config": "class Config" not in body,
            "model_config": "model_config = ConfigDict(from_attributes=True)" in body,
            "model_dump": "model_dump" in body,
            "no_dict": ".dict(" not in body,
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            print(f"fixture residue failed: {failed}\n{body}", file=sys.stderr)
            return 1
        print(f"apply+watch {elapsed:.3f}s ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
