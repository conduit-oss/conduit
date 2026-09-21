"""Live remint pydantic packet; never prints secret values."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "examples" / "sample-packet" / "pydantic-llm-mint-live.json"
ENV_PATH = ROOT / ".env"
REPORT = ROOT / "docs" / "smoke-tests" / "p6-remint-full-surface.json"


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


def _scrub(text: str, keys: list[str]) -> str:
    out = text
    for k in keys:
        val = os.environ.get(k) or ""
        if len(val) > 8:
            out = out.replace(val, f"<{k}>")
    return out


def main() -> int:
    present = _load_dotenv(ENV_PATH)
    print("env_keys_present", present)
    if not any(k.endswith("_KEY") or k == "OPENAI_API_KEY" for k in present):
        # OPENAI_API_KEY is the usual name
        if "OPENAI_API_KEY" not in present and "CONDUIT_LLM_API_KEY" not in present:
            print("no LLM API key in env", file=sys.stderr)
            return 2

    candidates = [
        ROOT / ".venv" / "Scripts" / "conduit.exe",
        ROOT / ".venv" / "bin" / "conduit",
        Path(sys.executable).with_name("conduit.exe"),
        Path(sys.executable).with_name("conduit"),
    ]
    bin_name = "conduit"
    for conduit in candidates:
        if conduit.is_file():
            bin_name = str(conduit)
            break

    # Backup previous freeze
    if OUT.is_file():
        bak = OUT.with_suffix(".json.prev")
        bak.write_bytes(OUT.read_bytes())
        print("backed_up", bak.name)

    cmd = [
        bin_name,
        "packet",
        "new",
        "--package",
        "pydantic",
        "--ecosystem",
        "pypi",
        "--version",
        "2.0.0",
        "--source-url",
        "https://docs.pydantic.dev/2.0/migration/",
        "--out",
        str(OUT),
    ]
    fixture = ROOT / "examples" / "pydantic-validator-fixture"
    if fixture.is_dir():
        cmd.extend(["--path", str(fixture)])
        print("consumer_path", fixture.as_posix())
    print("mint_cmd", " ".join(cmd))
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )
    elapsed = time.perf_counter() - t0
    print("mint_exit", proc.returncode)
    print("mint_wall_s", round(elapsed, 3))
    print("mint_out")
    print(_scrub((proc.stdout or "") + (proc.stderr or ""), present)[-6000:])
    if proc.returncode != 0:
        return proc.returncode
    if not OUT.is_file():
        print("packet missing", file=sys.stderr)
        return 1

    data = json.loads(OUT.read_text(encoding="utf-8"))
    rules = [r for r in (data.get("rules") or []) if isinstance(r, dict)]
    types = Counter(str(r.get("type") or "") for r in rules)
    call_site = sum(1 for t, n in types.items() if t and t != "DEPENDENCY_BUMP" for _ in range(n))
    # recount properly
    call_site = sum(1 for r in rules if str(r.get("type") or "") not in {"", "DEPENDENCY_BUMP"})
    surfaces = sum(
        1
        for r in rules
        if r.get("type") == "AST_CALL_REWRITE" and isinstance(r.get("surface"), dict)
    )
    callees = [
        str(r.get("old_callee") or "")
        for r in rules
        if r.get("type") == "AST_CALL_REWRITE"
    ]
    interesting = [
        c
        for c in callees
        if any(
            tok in c.lower()
            for tok in ("validator", "dict", "parse", "schema", "config", "orm")
        )
    ]
    effects = data.get("side_effects") or []
    effect_blob = " ".join(
        str(e.get("detail") or "") for e in effects if isinstance(e, dict)
    ).lower()
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()

    report = {
        "sha256": digest,
        "mint_exit": proc.returncode,
        "mint_wall_s": round(elapsed, 3),
        "rule_types": dict(types),
        "call_site_rules": call_site,
        "ast_call_with_surface": surfaces,
        "old_callees": callees,
        "interesting_callees": interesting,
        "side_effect_count": len(effects) if isinstance(effects, list) else 0,
        "side_effects_mention_config": "config" in effect_blob or "orm" in effect_blob,
        "side_effects_mention_validator": "validator" in effect_blob,
        "provider": os.environ.get("CONDUIT_LLM_PROVIDER") or "openai?",
        "model": os.environ.get("CONDUIT_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "",
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("report", REPORT.as_posix())
    print(json.dumps(report, indent=2))
    if call_site < 1:
        print("thin mint: zero call-site rules", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
