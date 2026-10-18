#!/usr/bin/env python3
"""Mine real dependency-bump + call-site-change cases from public GitHub repos.

Finds merged PRs / commits that bump a Python dependency (requirements*,
pyproject.toml, setup.py, Pipfile, poetry.lock) and also change Python call
sites of that library in the same change. The deleted lines that reference the
library are the ground truth. A case is kept only when the classifier says the
distribution is an outside service — scoring never consults a package name list.

Uses ``gh api`` when available (honours ``gh`` auth), else urllib with
``GITHUB_TOKEN`` / ``GH_TOKEN``.

Usage:
  python scripts/mine_known_breaks.py --out docs/known-breaks-mined.json --limit 100
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "conduit" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "conduit" / "src"))

# Seed queries discover candidates only. Scoring never reads this list.
_PR_QUERIES = (
    "sqlalchemy 2.0 is:pr is:merged language:Python",
    "bump sqlalchemy is:pr is:merged",
    "upgrade sqlalchemy is:pr is:merged",
    "pymongo 4 is:pr is:merged language:Python",
    "bump pymongo is:pr is:merged",
    "stripe-python OR \"bump stripe\" is:pr is:merged",
    "bump stripe is:pr is:merged language:Python",
    "boto3 bump is:pr is:merged language:Python",
    "upgrade boto3 is:pr is:merged",
    "openai 1.0 is:pr is:merged language:Python",
    "bump openai is:pr is:merged",
    "redis-py OR \"bump redis\" is:pr is:merged language:Python",
    "elasticsearch 8 is:pr is:merged language:Python",
    "psycopg2 bump is:pr is:merged language:Python",
    "anthropic bump is:pr is:merged language:Python",
    "google-cloud-storage bump is:pr is:merged",
    "pinecone bump is:pr is:merged language:Python",
    "motor bump is:pr is:merged language:Python",
    "cassandra-driver bump is:pr is:merged",
    "influxdb bump is:pr is:merged language:Python",
    "bump neo4j is:pr is:merged language:Python",
    "bump clickhouse is:pr is:merged language:Python",
    "bump snowflake-connector is:pr is:merged",
    "bump pymysql is:pr is:merged language:Python",
    "bump aiobotocore is:pr is:merged language:Python",
    "bump twilio is:pr is:merged language:Python",
    "bump sendgrid is:pr is:merged language:Python",
    "bump httpx is:pr is:merged NOT transport language:Python",
    "bump supabase is:pr is:merged language:Python",
    "bump firebase-admin is:pr is:merged language:Python",
    "bump discord.py is:pr is:merged language:Python",
    "bump praw is:pr is:merged language:Python",
    "bump tweepy is:pr is:merged language:Python",
    "bump slack-sdk is:pr is:merged language:Python",
    "bump kubernetes is:pr is:merged language:Python",
    "dependabot sqlalchemy is:pr is:merged",
    "dependabot pymongo is:pr is:merged",
    "dependabot stripe is:pr is:merged",
    "dependabot boto3 is:pr is:merged",
    "dependabot openai is:pr is:merged",
    "dependabot redis is:pr is:merged",
    "dependabot elasticsearch is:pr is:merged",
    "dependabot anthropic is:pr is:merged",
    "dependabot pinecone is:pr is:merged",
    "dependabot psycopg is:pr is:merged",
    "dependabot motor is:pr is:merged",
    "dependabot twilio is:pr is:merged",
    "dependabot supabase is:pr is:merged",
    "dependabot firebase-admin is:pr is:merged",
    "bump from sqlalchemy is:pr is:merged",
    "bump from pymongo is:pr is:merged",
    "bump from stripe is:pr is:merged",
    "bump from openai is:pr is:merged",
    "bump from boto3 is:pr is:merged",
    "migrate sqlalchemy 2 is:pr is:merged",
    "migrate pymongo 4 is:pr is:merged",
    "stripe-python 12 is:pr is:merged",
    "openai v1 migration is:pr is:merged",
)

_DEP_BASENAMES = {
    "requirements.txt",
    "requirements-dev.txt",
    "requirements.in",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
}

_PIN_RE = re.compile(
    r"(?i)^\s*(?:-\s*e\s+)?([A-Za-z0-9_.-]+)\s*(==|>=|<=|~=|!=|>|<)\s*([^\s;#\\]+)",
)
_TOML_PIN_RE = re.compile(
    r'(?i)^\s*([A-Za-z0-9_.-]+)\s*=\s*["\']([^"\']+)["\']',
)
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def _token() -> str:
    env = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if env:
        # Invalid env tokens override gh; probe quickly.
        return env
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except OSError:
        pass
    return ""


def _gh_api(path: str, *, accept: str | None = None) -> object | None:
    """Prefer ``gh api`` so host credentials work even when env tokens are bad."""
    cmd = ["gh", "api", path]
    if accept:
        cmd.extend(["-H", f"Accept: {accept}"])
    env = os.environ.copy()
    # Avoid a stale/invalid GITHUB_TOKEN shadowing gh's stored creds.
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if key in env and "Bad credentials" in (env.get("_GH_LAST_ERR") or ""):
            env.pop(key, None)
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "Bad credentials" in err or "401" in err:
            env.pop("GITHUB_TOKEN", None)
            env.pop("GH_TOKEN", None)
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _api(url: str, *, accept: str = "application/vnd.github+json") -> object | None:
    if url.startswith("https://api.github.com/"):
        path = url[len("https://api.github.com/") :]
        hit = _gh_api(path, accept=accept)
        if hit is not None:
            return hit
    headers = {
        "Accept": accept,
        "User-Agent": "conduit-mine-known-breaks",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _search_prs(query: str, *, page: int = 1) -> list[dict]:
    q = urllib.parse.quote(query)
    url = f"https://api.github.com/search/issues?q={q}&per_page=30&page={page}"
    payload = _api(url)
    if not isinstance(payload, dict):
        return []
    return [item for item in (payload.get("items") or []) if isinstance(item, dict)]


def _pr_detail(repo: str, number: int) -> dict | None:
    payload = _api(f"https://api.github.com/repos/{repo}/pulls/{number}")
    return payload if isinstance(payload, dict) else None


def _pr_files(repo: str, number: int) -> list[dict]:
    payload = _api(f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page=100")
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _file_at(repo: str, ref: str, path: str) -> str:
    quoted = urllib.parse.quote(path)
    payload = _api(f"https://api.github.com/repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref)}")
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if not isinstance(content, str):
        return ""
    try:
        return base64.b64decode(content).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_pins(text: str, filename: str) -> dict[str, str]:
    found: dict[str, str] = {}
    lower = filename.lower()
    for line in text.splitlines():
        if lower.endswith(".toml"):
            match = _TOML_PIN_RE.match(line)
            if match and any(ch.isdigit() for ch in match.group(2)):
                name = match.group(1).lower().replace("_", "-")
                ver = _VERSION_RE.search(match.group(2))
                if ver:
                    found[name] = ver.group(1)
            continue
        match = _PIN_RE.match(line)
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        ver = _VERSION_RE.search(match.group(3))
        if ver:
            found[name] = ver.group(1)
    return found


def _major_minor(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split(".")[:2] if p.isdigit())


def _is_major_or_minor_bump(old: str, new: str) -> bool:
    a, b = _major_minor(old), _major_minor(new)
    if len(a) < 1 or len(b) < 1:
        return False
    if a[0] != b[0]:
        return True
    return len(a) > 1 and len(b) > 1 and a[1] != b[1]


def _library_refs(name: str) -> tuple[str, ...]:
    raw = name.lower().replace("-", "_")
    tops = {raw, name.lower()}
    if "_" in raw:
        tops.add(raw.split("_", 1)[0])
    if "-" in name:
        tops.add(name.lower().split("-", 1)[0])
    return tuple(sorted(t for t in tops if len(t) > 2))


def _line_refs_library(line: str, refs: tuple[str, ...]) -> bool:
    lower = line.lower()
    stripped = lower.strip()
    if not stripped or stripped.startswith("#"):
        return False
    for ref in refs:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![A-Za-z0-9_])", lower):
            return True
    return False


def _extract_py_hits(patch: str, *, refs: tuple[str, ...], filename: str) -> list[dict]:
    hits: list[dict] = []
    if not patch:
        return hits
    old_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = re.search(r"@@ -(\d+)", raw)
            if match:
                old_line = int(match.group(1))
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw.startswith("-"):
            body = raw[1:]
            if body.strip() and _line_refs_library(body, refs):
                hits.append({"file": filename, "line": old_line, "symbol": body.strip()[:160]})
            old_line += 1
        elif raw.startswith("+"):
            continue
        else:
            old_line += 1
    return hits


_CLASSIFY_MEMO: dict[tuple[str, str], bool] = {}


def _classify_outside(name: str, version: str) -> bool:
    key = (name.lower(), version)
    hit = _CLASSIFY_MEMO.get(key)
    if hit is not None:
        return hit
    try:
        from conduit.outside_in.evidence import classify_distribution
    except Exception:
        _CLASSIFY_MEMO[key] = False
        return False
    found = classify_distribution(name, version)
    ok = bool(found.service and not found.transport)
    _CLASSIFY_MEMO[key] = ok
    return ok


def _repo_from_issue(item: dict) -> str:
    url = str(item.get("repository_url") or "")
    # https://api.github.com/repos/owner/name
    if "/repos/" in url:
        return url.split("/repos/", 1)[1]
    html = str(item.get("html_url") or "")
    parts = html.split("/")
    if len(parts) >= 5 and "github.com" in html:
        return f"{parts[3]}/{parts[4]}"
    return ""


def _process_pr(item: dict, *, seen: set[str]) -> dict | None:
    repo = _repo_from_issue(item)
    number = int(item.get("number") or 0)
    if not repo or not number:
        return None
    key = f"{repo}#{number}"
    if key in seen:
        return None
    seen.add(key)
    detail = _pr_detail(repo, number)
    if not detail or not detail.get("merged_at"):
        return None
    merge_sha = str(detail.get("merge_commit_sha") or "")
    base_sha = str((detail.get("base") or {}).get("sha") or "")
    if not merge_sha or not base_sha:
        return None
    files = _pr_files(repo, number)
    dep_files = []
    py_files = []
    for entry in files:
        path = str(entry.get("filename") or "")
        base = path.rsplit("/", 1)[-1]
        if base in _DEP_BASENAMES or base.startswith("requirements"):
            dep_files.append(entry)
        elif path.endswith(".py"):
            py_files.append(entry)
    if not dep_files or not py_files:
        return None

    bumps: list[tuple[str, str, str, str]] = []
    for entry in dep_files:
        path = str(entry.get("filename") or "")
        # Prefer the PR patch when it carries enough pin lines (avoids two content fetches).
        patch = str(entry.get("patch") or "")
        before_lines = []
        after_lines = []
        if patch:
            for raw in patch.splitlines():
                if raw.startswith("-") and not raw.startswith("---"):
                    before_lines.append(raw[1:])
                elif raw.startswith("+") and not raw.startswith("+++"):
                    after_lines.append(raw[1:])
        if before_lines or after_lines:
            old_pins = _parse_pins("\n".join(before_lines), path)
            new_pins = _parse_pins("\n".join(after_lines), path)
        else:
            before = _file_at(repo, base_sha, path)
            after = _file_at(repo, merge_sha, path)
            if not before or not after:
                continue
            old_pins = _parse_pins(before, path)
            new_pins = _parse_pins(after, path)
        for dist, new_ver in new_pins.items():
            old_ver = old_pins.get(dist)
            if not old_ver or old_ver == new_ver:
                continue
            if not _is_major_or_minor_bump(old_ver, new_ver):
                continue
            bumps.append((dist, old_ver, new_ver, path))
    if not bumps:
        return None

    for dist, old_ver, new_ver, pin_file in bumps:
        refs = _library_refs(dist)
        expected: list[dict] = []
        for entry in py_files:
            path = str(entry.get("filename") or "")
            patch = str(entry.get("patch") or "")
            expected.extend(_extract_py_hits(patch, refs=refs, filename=path))
        uniq: dict[tuple[str, int], dict] = {}
        for hit in expected:
            uniq[(hit["file"], hit["line"])] = hit
        expected = list(uniq.values())
        if not expected:
            continue
        # Classifier last: only download/scan when the PR already has call-site edits.
        if not _classify_outside(dist, old_ver):
            continue
        if len(expected) > 40:
            expected = expected[:40]
        return {
            "repo": repo,
            "url": f"https://github.com/{repo}",
            "commit": base_sha,
            "parent": base_sha,
            "merge_commit": merge_sha,
            "pr": number,
            "subdir": "",
            "service": dist,
            "pin": f"{dist}=={old_ver}",
            "pin_file": pin_file,
            "from_version": old_ver,
            "to_version": new_ver,
            "expected": expected,
        }
    return None


def mine(*, limit: int, out: Path) -> dict:
    # Drop invalid env tokens so gh host auth wins.
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(key, "")
        if val:
            # Probe
            probe = _gh_api("rate_limit")
            if probe is None:
                os.environ.pop(key, None)

    seen: set[str] = set()
    cases: list[dict] = []
    libraries: dict[str, int] = {}
    for query in _PR_QUERIES:
        if len(cases) >= limit:
            break
        for page in range(1, 6):
            if len(cases) >= limit:
                break
            items = _search_prs(query, page=page)
            if not items:
                break
            for item in items:
                if len(cases) >= limit:
                    break
                try:
                    found = _process_pr(item, seen=seen)
                except Exception as exc:
                    print(f"skip: {exc}", file=sys.stderr)
                    continue
                if found is None:
                    continue
                svc = str(found["service"])
                repo_name = str(found["repo"])
                per_lib_cap = 20 if len(libraries) >= 8 else 15
                if libraries.get(svc, 0) >= per_lib_cap:
                    continue
                same_repo = sum(1 for case in cases if case["repo"] == repo_name)
                if same_repo >= 3:
                    continue
                cases.append(found)
                libraries[svc] = libraries.get(svc, 0) + 1
                print(
                    f"[{len(cases)}] {found['repo']}#{found['pr']} {svc} "
                    f"{found['from_version']}->{found['to_version']} "
                    f"lines={len(found['expected'])} libs={len(libraries)}",
                    flush=True,
                )
                _write_payload(out, cases, libraries)
            time.sleep(0.4)

    _write_payload(out, cases, libraries)
    print(f"wrote {out} cases={len(cases)} libraries={sorted(libraries)}")
    return json.loads(out.read_text(encoding="utf-8"))


def _write_payload(out: Path, cases: list[dict], libraries: dict[str, int]) -> dict:
    payload = {
        "note": (
            "Mined automatically from public GitHub pull requests that bump a dependency "
            "major/minor and change Python call sites of that library. Expected file:line "
            "pairs are deleted lines in the PR that reference the library. Written before "
            "any conduit impact run. Do not edit after scoring. Precision against this "
            "ground truth undercounts because developers do not always fix every broken "
            "line in the same PR."
        ),
        "scoring": {
            "catch": "A non-wrapper impact site whose path ends with the expected file and whose line equals the expected line.",
            "miss": "An expected site with no such impact site.",
            "false_positive": (
                "A non-wrapper impact site that is not one of the expected file:line pairs."
            ),
        },
        "repos": cases,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "known-breaks-mined.json")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    # Prefer gh host credentials over a broken env token from the cloud agent.
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GH_TOKEN", None)
    mine(limit=args.limit, out=args.out)


if __name__ == "__main__":
    main()
