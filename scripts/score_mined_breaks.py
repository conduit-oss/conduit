#!/usr/bin/env python3
"""Score docs/known-breaks-mined-v2.json with conduit impact; leave ground truth untouched.

Each case runs in its own ``conduit.main impact`` subprocess (via known_break_eval),
sequentially so peak RSS stays bounded. Every row ends as scored, not compared,
or not an outside service. Writes docs/known-breaks-mined-results.json.
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
    parser.add_argument("--ground", type=Path, default=ROOT / "docs" / "known-breaks-mined-v2.json")
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
        print(
            f"== {repo['repo']} {repo['service']} {repo.get('from_version')}->{repo.get('to_version')}",
            flush=True,
        )
        try:
            project = eval_mod._checkout(repo)
        except SystemExit as exc:
            rows.append(
                {
                    "repo": repo["repo"],
                    "service": repo["service"],
                    "versions": f"{repo.get('from_version')} -> {repo.get('to_version')}",
                    "status": "not compared",
                    "not_compared_reason": f"checkout failed: {exc}",
                    "expected": len(repo.get("expected") or []),
                    "caught": 0,
                    "missed": len(repo.get("expected") or []),
                    "false_positives": 0,
                    "recall": f"0/{len(repo.get('expected') or [])}",
                    "precision": "n/a",
                    "caught_sites": [],
                    "missed_sites": list(repo.get("expected") or []),
                    "false_positive_sites": [],
                    "notes": [str(exc)],
                }
            )
            print(f"   not compared: checkout failed", flush=True)
            continue
        payload = eval_mod._impact(project, repo["service"], repo)
        row = eval_mod._score(repo, payload)
        if not row.get("versions"):
            raise SystemExit(f"scorer bug: blank versions for {repo['repo']} {repo['service']}")
        rows.append(row)
        print(
            f"   {row.get('status')} recall {row['recall']} precision {row['precision']} "
            f"versions {row.get('versions')} {row.get('not_compared_reason') or ''}".strip(),
            flush=True,
        )
    by_lib: dict[str, dict] = {}
    status_counts = {"scored": 0, "not compared": 0, "not an outside service": 0}
    for row in rows:
        status = row.get("status") or "scored"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "scored":
            continue
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
    miss_sample = []
    for row in rows:
        if row.get("status") != "scored":
            continue
        for site in (row.get("false_positive_sites") or [])[:3]:
            fp_sample.append(
                {
                    "repo": row["repo"],
                    "service": row["service"],
                    "path": site.get("path"),
                    "line": site.get("line"),
                    "expr": site.get("expr"),
                    "change": site.get("change"),
                    "versions": row.get("versions"),
                }
            )
            if len(fp_sample) >= 40:
                break
        for site in (row.get("missed_sites") or [])[:3]:
            miss_sample.append(
                {
                    "repo": row["repo"],
                    "service": row["service"],
                    "file": site.get("file"),
                    "line": site.get("line"),
                    "symbol": site.get("symbol"),
                    "versions": row.get("versions"),
                }
            )
            if len(miss_sample) >= 40:
                break
        if len(fp_sample) >= 40 and len(miss_sample) >= 40:
            break
    payload = {
        "ground": str(args.ground.relative_to(ROOT)) if args.ground.is_relative_to(ROOT) else str(args.ground),
        "rows": rows,
        "by_library": by_lib,
        "status_counts": status_counts,
        "false_positive_sample": fp_sample,
        "miss_sample": miss_sample,
        "totals": {
            "cases": len(rows),
            "expected": sum(int(r.get("expected") or 0) for r in rows if r.get("status") == "scored"),
            "caught": sum(int(r.get("caught") or 0) for r in rows if r.get("status") == "scored"),
            "false_positives": sum(
                int(r.get("false_positives") or 0) for r in rows if r.get("status") == "scored"
            ),
        },
    }
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    print("status_counts", json.dumps(status_counts))
    print("by_library", json.dumps(by_lib, indent=2))


if __name__ == "__main__":
    main()
