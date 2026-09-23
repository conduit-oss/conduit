#!/usr/bin/env python3
"""Prove watch → apply → watch on an isolated pydantic validator fixture copy.

Consumer-migration kill bar for the declared fixture (not unbounded full v2).
Seeds pin at to_version so the first Watch is bump_dirty, then apply, then clean.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_repo() -> Path:
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "conduit" / "src" / "conduit").is_dir():
            return cand
    raise SystemExit("could not locate repo root")


def run_cli(
    repo: Path, args: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "conduit" / "src")
    cmd = [sys.executable, "-m", "conduit.main", *args]
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    repo = find_repo()
    skill = repo / ".cursor" / "skills" / "verify-conduit"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = skill / "evidence" / f"consumer-migrate-pydantic-{run_id}"
    scratch = skill / "scratch" / f"pydantic-fixture-{run_id}"
    evidence.mkdir(parents=True, exist_ok=True)

    packet = repo / "examples" / "sample-packet" / "pydantic-surface-hop.json"
    source = repo / "examples" / "pydantic-validator-fixture"

    # Isolate
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(
        source,
        scratch,
        ignore=shutil.ignore_patterns(
            ".pytest_cache", "__pycache__", ".conduit", ".venv", "venv", ".git"
        ),
    )
    # Seed pin at to_version so pre-apply Watch is bump_dirty (exit 1)
    (scratch / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")

    write(evidence / "run.cmd.txt", f"prove_watch_apply_watch packet={packet.name}\n")
    write(evidence / "tree.before.txt", (scratch / "src" / "model.py").read_text(encoding="utf-8"))

    watch1 = run_cli(
        repo,
        ["watch", "--path", str(scratch), "--packet", str(packet), "--json"],
        cwd=repo,
    )
    write(evidence / "watch-before.stdout.txt", watch1.stdout)
    write(evidence / "watch-before.stderr.txt", watch1.stderr)
    write(evidence / "watch-before.exitcode.txt", str(watch1.returncode))

    apply = run_cli(
        repo,
        ["apply", "--path", str(scratch), "--packet", str(packet)],
        cwd=repo,
    )
    write(evidence / "apply.stdout.txt", apply.stdout)
    write(evidence / "apply.stderr.txt", apply.stderr)
    write(evidence / "apply.exitcode.txt", str(apply.returncode))
    write(evidence / "tree.after.txt", (scratch / "src" / "model.py").read_text(encoding="utf-8"))

    watch2 = run_cli(
        repo,
        ["watch", "--path", str(scratch), "--packet", str(packet), "--json"],
        cwd=repo,
    )
    write(evidence / "watch-after.stdout.txt", watch2.stdout)
    write(evidence / "watch-after.stderr.txt", watch2.stderr)
    write(evidence / "watch-after.exitcode.txt", str(watch2.returncode))

    after_body = (scratch / "src" / "model.py").read_text(encoding="utf-8")
    checks = {
        "watch_before_exit": watch1.returncode,
        "apply_exit": apply.returncode,
        "watch_after_exit": watch2.returncode,
        "has_field_validator": "@field_validator" in after_body,
        "has_classmethod": "@classmethod" in after_body,
        "has_model_dump": "model_dump" in after_body,
        "has_model_config_or_configdict": (
            "model_config" in after_body or "ConfigDict" in after_body
        ),
        "no_class_config": "class Config" not in after_body,
        "no_bare_validator_decorator": '@validator("name")' not in after_body,
    }
    try:
        payload1 = json.loads(watch1.stdout)
        checks["watch_before_status"] = payload1.get("status")
        checks["watch_before_leftover_count"] = payload1.get("leftover_count")
        payload = json.loads(watch2.stdout)
        checks["watch_after_status"] = payload.get("status")
        checks["watch_after_leftovers"] = payload.get("leftovers")
        checks["watch_after_leftover_count"] = payload.get("leftover_count")
    except Exception as exc:
        checks["watch_json_error"] = str(exc)

    write(evidence / "verdict.json", json.dumps(checks, indent=2) + "\n")

    ok = (
        watch1.returncode == 1
        and checks.get("watch_before_status") == "bump_dirty"
        and (checks.get("watch_before_leftover_count") or 0) > 0
        and apply.returncode == 0
        and watch2.returncode == 0
        and checks.get("watch_after_status") == "clean"
        and checks.get("watch_after_leftover_count") == 0
        and checks["has_field_validator"]
        and checks["has_classmethod"]
        and checks["has_model_dump"]
        and checks["has_model_config_or_configdict"]
        and checks["no_class_config"]
    )
    write(evidence / "RESULT.txt", "PASS\n" if ok else "FAIL\n")
    print(f"evidence={evidence}")
    print(f"scratch={scratch}")
    print(json.dumps(checks, indent=2))
    print("RESULT=PASS" if ok else "RESULT=FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
