#!/usr/bin/env python3
"""Score live conduit impact against docs/known-breaks.json.

The expected file:line pairs are an input. This script does not edit them.
It clones each commit, runs ``conduit impact`` (live wheels) and ``conduit watch``,
and writes a score JSON next to the ground truth.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ROOT = Path(__file__).resolve().parents[1]
GROUND = ROOT / "docs" / "known-breaks.json"
DEFAULT_OUT = ROOT / "docs" / "known-breaks-results.json"
CHECKOUTS = Path("/tmp/known-breaks")


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=env,
    )


def _checkout(repo: dict) -> Path:
    local = repo.get("local_path")
    if local:
        path = Path(local)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_dir():
            raise SystemExit(f"missing local_path {path}")
        return path
    dest = CHECKOUTS / repo["repo"].replace("/", "__")
    if not (dest / ".git").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = [
            "git",
            "clone",
            "--filter=blob:none",
            "--depth",
            "1",
        ]
        if repo.get("subdir"):
            clone.append("--sparse")
        clone.extend([repo["url"] + ".git", str(dest)])
        proc = _run(clone)
        if proc.returncode != 0:
            raise SystemExit(f"clone {repo['repo']} failed\n{proc.stdout}")
        if repo.get("subdir"):
            sparse = _run(["git", "sparse-checkout", "set", repo["subdir"]], cwd=dest)
            if sparse.returncode != 0:
                raise SystemExit(sparse.stdout)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=dest).stdout.strip()
    if not sha.startswith(repo["commit"]):
        fetch = _run(["git", "fetch", "--depth", "1", "origin", repo["commit"]], cwd=dest)
        if fetch.returncode != 0:
            raise SystemExit(f"fetch {repo['repo']} {repo['commit']} failed\n{fetch.stdout}")
        checkout = _run(["git", "checkout", "--detach", repo["commit"]], cwd=dest)
        if checkout.returncode != 0:
            raise SystemExit(checkout.stdout)
    project = dest / repo["subdir"] if repo.get("subdir") else dest
    if not project.is_dir():
        raise SystemExit(f"missing subdir {project}")
    return project


def _parse_payload(text: str) -> dict | None:
    """Impact JSON follows SyntaxWarning lines. Those warnings can contain ``{``."""
    clean = _ANSI.sub("", text)
    starts = [match.start() for match in re.finditer(r"(?m)^\{", clean)]
    if not starts and "{" in clean:
        starts = [clean.find("{")]
    for start in reversed(starts):
        try:
            return json.loads(clean[start:])
        except json.JSONDecodeError:
            continue
    return None


def _impact(project: Path, service: str, repo: dict | None = None) -> dict:
    cmd = [
        sys.executable,
        "-m",
        "conduit.main",
        "impact",
        "--path",
        str(project),
        "--dep",
        service,
        "--json",
    ]
    if repo:
        from_version = str(repo.get("from_version") or "").strip()
        to_version = str(repo.get("to_version") or repo.get("removed_in") or "").strip()
        if from_version:
            cmd.extend(["--from", from_version])
        if to_version:
            cmd.extend(["--to", to_version])
        fixtures = repo.get("fixtures")
        if fixtures:
            cmd.extend(["--fixtures", str(ROOT / fixtures)])
    proc = _run(cmd, cwd=ROOT)
    payload = _parse_payload(proc.stdout)
    if payload is None:
        return {"impacts": [], "notes": [proc.stdout[-2000:]], "exit": proc.returncode}
    payload["exit"] = proc.returncode
    return payload


def _watch(project: Path, service: str) -> str:
    proc = _run(
        [
            sys.executable,
            "-m",
            "conduit.main",
            "watch",
            "--path",
            str(project),
            "--service",
            service,
        ],
        cwd=ROOT,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return "\n".join(lines[:40])


def _site_key(path: str, line: int) -> tuple[str, int]:
    return (path.replace("\\", "/"), line)


def _matches(site: dict, expected: dict) -> bool:
    path = str(site.get("path") or "").replace("\\", "/")
    line = int(site.get("line") or 0)
    return path.endswith(expected["file"]) and line == expected["line"]


def _score(repo: dict, payload: dict) -> dict:
    impacts = payload.get("impacts") or []
    primary: list[dict] = []
    for impact in impacts:
        if str(impact.get("kind") or "") == "deprecated":
            continue
        for site in impact.get("sites") or []:
            if site.get("via_wrapper"):
                continue
            primary.append(
                {
                    "path": site.get("path"),
                    "line": site.get("line"),
                    "expr": site.get("expr"),
                    "change": impact.get("change"),
                    "from_version": impact.get("from_version"),
                    "to_version": impact.get("to_version"),
                }
            )
    caught = []
    missed = []
    for item in repo["expected"]:
        hit = next((site for site in primary if _matches(site, item)), None)
        row = {"file": item["file"], "line": item["line"], "symbol": item["symbol"]}
        if hit:
            caught.append({**row, "change": hit["change"], "expr": hit["expr"]})
        else:
            missed.append(row)
    expected_keys = {_site_key(item["file"], item["line"]) for item in repo["expected"]}
    false_positives = []
    for site in primary:
        path = str(site["path"] or "")
        line = int(site["line"] or 0)
        if any(path.endswith(item["file"]) and line == item["line"] for item in repo["expected"]):
            continue
        if _site_key(path, line) in expected_keys:
            continue
        false_positives.append(site)
    versions = ""
    if primary:
        versions = f"{primary[0]['from_version']} -> {primary[0]['to_version']}"
    elif impacts:
        versions = f"{impacts[0].get('from_version')} -> {impacts[0].get('to_version')}"
    expected_n = len(repo["expected"])
    note_text = "\n".join(str(item) for item in (payload.get("notes") or []))
    if payload.get("exit") not in (0, None) and "not an outside service" in note_text.lower():
        return {
            "repo": repo["repo"],
            "service": repo["service"],
            "pin": repo["pin"],
            "versions": "",
            "status": "not an outside service",
            "expected": expected_n,
            "caught": 0,
            "missed": 0,
            "false_positives": 0,
            "recall": "not an outside service",
            "precision": "n/a",
            "caught_sites": [],
            "missed_sites": [],
            "false_positive_sites": [],
            "notes": payload.get("notes") or [],
            "exit": payload.get("exit"),
        }
    return {
        "repo": repo["repo"],
        "service": repo["service"],
        "pin": repo["pin"],
        "versions": versions,
        "expected": expected_n,
        "caught": len(caught),
        "missed": len(missed),
        "false_positives": len(false_positives),
        "recall": f"{len(caught)}/{expected_n}",
        "precision": (
            f"{len(caught)}/{len(caught) + len(false_positives)}"
            if (caught or false_positives)
            else "n/a"
        ),
        "caught_sites": caught,
        "missed_sites": missed,
        "false_positive_sites": false_positives[:30],
        "notes": payload.get("notes") or [],
        "exit": payload.get("exit"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ground", type=Path, default=GROUND)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--only", action="append", default=[], help="repo slug substring")
    parser.add_argument("--skip-watch", action="store_true")
    args = parser.parse_args()
    ground = json.loads(args.ground.read_text(encoding="utf-8"))
    rows = []
    for repo in ground["repos"]:
        if args.only and not any(part in repo["repo"] for part in args.only):
            continue
        print(f"== {repo['repo']} {repo['service']}", flush=True)
        project = _checkout(repo)
        payload = _impact(project, repo["service"], repo)
        row = _score(repo, payload)
        if not args.skip_watch:
            row["watch_head"] = _watch(project, repo["service"])
        rows.append(row)
        print(
            f"   {row.get('status') or ''} recall {row['recall']} precision {row['precision']} versions {row['versions']}".strip(),
            flush=True,
        )
    args.out.write_text(json.dumps({"rows": rows}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
