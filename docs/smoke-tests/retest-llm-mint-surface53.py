"""Retest frozen P6 live packet on main+#53 surface binder. Never prints secrets."""

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
EXPECTED_SHA256 = "e5ab11b54ff4927ccf9d3e818a2bf1a3bd31c8ba736250f7bf0b493025e32a8d"
ENV_PATH = ROOT / ".env"


def _load_dotenv(path: Path) -> list[str]:
    present: list[str] = []
    if not path.is_file():
        return present
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value
        if key in {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "CONDUIT_LLM_API_KEY",
            "CONDUIT_LLM_PROVIDER",
            "CONDUIT_LLM_MODEL",
        } and os.environ.get(key):
            present.append(key)
    return sorted(set(present))


def _conduit() -> str:
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
        env=os.environ.copy(),
    )


def _residue(src: Path) -> dict[str, int]:
    text = src.read_text(encoding="utf-8")
    return {
        "validator_decorator": text.count("@validator"),
        "dict_call": text.count(".dict("),
        "model_dump": text.count("model_dump"),
        "class_Config": text.count("class Config"),
        "orm_mode": text.count("orm_mode"),
    }


def main() -> int:
    present = _load_dotenv(ENV_PATH)
    print("env_keys_present", present)

    raw = PACKET.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    print("packet_sha256", digest)
    if digest != EXPECTED_SHA256:
        print(f"checksum mismatch want {EXPECTED_SHA256}", file=sys.stderr)
        return 1

    conduit = _conduit()
    print("conduit", conduit)
    print("python", sys.executable)
    print("head_hint", "main+#53 surface binder retest")

    test = _run([conduit, "packet", "test", "--packet", str(PACKET)])
    print("packet_test_exit", test.returncode)
    if test.returncode != 0:
        print(test.stdout or test.stderr)
        return test.returncode

    with tempfile.TemporaryDirectory(prefix="conduit-p6-retest-") as tmp:
        tree = Path(tmp) / "consumer"
        shutil.copytree(FIXTURE, tree)
        src = tree / "src" / "model.py"
        pre = _residue(src)
        print("residue_pre", json.dumps(pre))

        t0 = time.perf_counter()
        apply = _run([conduit, "apply", "--path", str(tree), "--packet", str(PACKET)])
        watch = _run(
            [conduit, "watch", "--path", str(tree), "--packet", str(PACKET), "--json"]
        )
        elapsed = time.perf_counter() - t0

        apply_text = (apply.stdout or "") + (apply.stderr or "")
        watch_text = (watch.stdout or "") + (watch.stderr or "")
        print("apply_exit", apply.returncode)
        # Strip anything that looks like a key
        safe_apply = apply_text
        for k in present:
            val = os.environ.get(k) or ""
            if len(val) > 8:
                safe_apply = safe_apply.replace(val, f"<{k}>")
        print("apply_out")
        print(safe_apply[-4000:] if len(safe_apply) > 4000 else safe_apply)

        start = watch_text.find("{")
        end = watch_text.rfind("}")
        if start < 0 or end <= start:
            print("watch JSON missing", file=sys.stderr)
            print(watch_text[-2000:], file=sys.stderr)
            return 1
        payload = json.loads(watch_text[start : end + 1])
        print("watch_exit", watch.returncode)
        print("watch_status", payload.get("status"))
        print("watch_leftover_count", payload.get("leftover_count"))
        completeness = payload.get("completeness") or {}
        print("completeness_status", completeness.get("status"))
        print("completeness_obs", completeness.get("observation_count"))
        print(
            "completeness_reasons",
            json.dumps(completeness.get("reasons") or []),
        )
        obs = completeness.get("observations") or []
        for item in obs[:12]:
            print(
                "obs",
                item.get("chain"),
                item.get("evidence"),
                item.get("confidence"),
                item.get("spelling"),
            )

        post = _residue(src)
        print("residue_post", json.dumps(post))
        print("pin", (tree / "requirements.txt").read_text(encoding="utf-8").strip())
        print(f"apply_watch_s {elapsed:.3f}")

        if apply.returncode != 0:
            return apply.returncode
        if "packet enrichment" in apply_text.lower():
            print("unexpected enrich during apply", file=sys.stderr)
            return 1

        out = {
            "packet_sha256": digest,
            "apply_exit": apply.returncode,
            "watch_exit": watch.returncode,
            "watch": {
                "status": payload.get("status"),
                "leftover_count": payload.get("leftover_count"),
                "completeness": completeness,
            },
            "residue_pre": pre,
            "residue_post": post,
            "elapsed_s": round(elapsed, 3),
            "surface_definite": "SURFACE_DEFINITE_REWRITE" in apply_text,
            "gate_pass": apply.returncode == 0
            and payload.get("status") == "clean"
            and payload.get("leftover_count") == 0,
            "dict_rewritten": post["dict_call"] == 0 and post["model_dump"] >= 1,
        }
        out_path = ROOT / "docs" / "smoke-tests" / "p6-retest-surface-53.json"
        out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print("wrote", out_path.as_posix())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
