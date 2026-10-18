#!/usr/bin/env python3
"""Score docs/known-breaks-mined.json with conduit impact; leave ground truth untouched.

Writes docs/known-breaks-mined-results.json and a short FP sample for hand review.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import known_break_eval as eval_mod  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ground", type=Path, default=ROOT / "docs" / "known-breaks-mined.json")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "known-breaks-mined-results.json")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip-watch", action="store_true", default=True)
    args = parser.parse_args()
    ground = json.loads(args.ground.read_text(encoding="utf-8"))
    rows = []
    repos = ground["repos"]
    if args.only:
        repos = [repo for repo in repos if any(part in repo["repo"] for part in args.only)]
    if args.limit:
        repos = repos[: args.limit]
    for repo in repos:
        print(f"== {repo['repo']} {repo['service']} {repo.get('from_version')}->{repo.get('to_version')}", flush=True)
        try:
            project = eval_mod._checkout(repo)
        except SystemExit as exc:
            rows.append(
                {
                    "repo": repo["repo"],
                    "service": repo["service"],
                    "error": str(exc),
                    "expected": len(repo.get("expected") or []),
                    "caught": 0,
                    "missed": len(repo.get("expected") or []),
                    "false_positives": 0,
                    "recall": f"0/{len(repo.get('expected') or [])}",
                    "precision": "n/a",
                }
            )
            continue
        payload = eval_mod._impact(project, repo["service"], repo)
        row = eval_mod._score(repo, payload)
        rows.append(row)
        print(
            f"   recall {row['recall']} precision {row['precision']} versions {row.get('versions')}",
            flush=True,
        )
    by_lib: dict[str, dict] = {}
    for row in rows:
        svc = row["service"]
        bucket = by_lib.setdefault(
            svc,
            {"cases": 0, "expected": 0, "caught": 0, "false_positives": 0},
        )
        bucket["cases"] += 1
        bucket["expected"] += int(row.get("expected") or 0)
        bucket["caught"] += int(row.get("caught") or 0)
        bucket["false_positives"] += int(row.get("false_positives") or 0)
    for svc, bucket in by_lib.items():
        exp = bucket["expected"]
        caught = bucket["caught"]
        fp = bucket["false_positives"]
        bucket["recall"] = f"{caught}/{exp}" if exp else "n/a"
        bucket["precision"] = f"{caught}/{caught + fp}" if (caught or fp) else "n/a"
    fp_sample = []
    for row in rows:
        for site in (row.get("false_positive_sites") or [])[:3]:
            fp_sample.append(
                {
                    "repo": row["repo"],
                    "service": row["service"],
                    "path": site.get("path"),
                    "line": site.get("line"),
                    "expr": site.get("expr"),
                    "change": site.get("change"),
                }
            )
            if len(fp_sample) >= 20:
                break
        if len(fp_sample) >= 20:
            break
    payload = {
        "rows": rows,
        "by_library": by_lib,
        "false_positive_sample": fp_sample,
        "totals": {
            "cases": len(rows),
            "expected": sum(int(r.get("expected") or 0) for r in rows),
            "caught": sum(int(r.get("caught") or 0) for r in rows),
            "false_positives": sum(int(r.get("false_positives") or 0) for r in rows),
        },
    }
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    print("by_library", json.dumps(by_lib, indent=2))


if __name__ == "__main__":
    main()
