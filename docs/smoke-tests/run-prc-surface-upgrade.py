"""PR-C: apply/Watch after surface upgrade of live LLM-mint packet."""

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

ROOT = Path(__file__).resolve().parents[2]
PACKET = ROOT / "examples" / "sample-packet" / "pydantic-llm-mint-live.json"
FIXTURE = ROOT / "examples" / "pydantic-validator-fixture"
OUT = ROOT / "docs" / "smoke-tests" / "p6-prc-surface-upgrade.json"
ENV_PATH = ROOT / ".env"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    _load_dotenv(ENV_PATH)
    digest = hashlib.sha256(PACKET.read_bytes()).hexdigest()
    conduit = Path(sys.executable).with_name("conduit.exe")
    if not conduit.is_file():
        conduit = Path(sys.executable).with_name("conduit")
    bin_name = str(conduit) if conduit.is_file() else "conduit"

    with tempfile.TemporaryDirectory(prefix="p6-prc-") as tmp:
        tree = Path(tmp) / "consumer"
        shutil.copytree(FIXTURE, tree)
        src = tree / "src" / "model.py"
        pre = src.read_text(encoding="utf-8")
        t0 = time.perf_counter()
        apply = subprocess.run(
            [bin_name, "apply", "--path", str(tree), "--packet", str(PACKET)],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=False,
        )
        watch = subprocess.run(
            [
                bin_name,
                "watch",
                "--path",
                str(tree),
                "--packet",
                str(PACKET),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=False,
        )
        elapsed = time.perf_counter() - t0
        post = src.read_text(encoding="utf-8")
        apply_text = (apply.stdout or "") + (apply.stderr or "")
        watch_text = (watch.stdout or "") + (watch.stderr or "")
        start = watch_text.find("{")
        end = watch_text.rfind("}")
        payload = (
            json.loads(watch_text[start : end + 1])
            if start >= 0 and end > start
            else {}
        )
        out = {
            "sha256": digest,
            "apply_exit": apply.returncode,
            "watch_exit": watch.returncode,
            "watch_status": payload.get("status"),
            "leftover_count": payload.get("leftover_count"),
            "completeness": payload.get("completeness"),
            "dict_pre": pre.count(".dict("),
            "dict_post": post.count(".dict("),
            "model_dump_post": post.count("model_dump"),
            "surface_rewrite": "SURFACE_DEFINITE_REWRITE" in apply_text,
            "elapsed_s": round(elapsed, 3),
        }
        OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(out, indent=2))
        if apply.returncode != 0:
            return apply.returncode
        completeness = (payload.get("completeness") or {}).get("status")
        if completeness == "unverified":
            print("completeness still unverified", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
