"""Create a migration branch and open a Pull Request via git + gh."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conduit.patcher.engine import PatchReport
from conduit.test_runner import TestResult

GH_MISSING_MSG = (
    "gh not found; install it or use --skip-pr. "
    "GitHub CLI (https://cli.github.com/) installed and on PATH "
    "(gh --version in the same terminal you use for Conduit)"
)


@dataclass
class PRResult:
    branch: str
    title: str
    url: str | None
    created: bool
    message: str


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=127,
            stdout="",
            stderr=f"{cmd[0]} not found on PATH: {exc}",
        )


def _resolve_gh() -> str | None:
    if sys.platform == "win32":
        return shutil.which("gh.exe") or shutil.which("gh")
    return shutil.which("gh")


def _rule_summary(rule: dict[str, Any]) -> str:
    rtype = str(rule.get("type") or "")
    if rtype == "EXACT_STRING_REPLACE":
        return f"`{rule.get('match')}` → `{rule.get('replace')}`"
    if rtype == "REGEX_REPLACE":
        return f"regex `{rule.get('pattern')}` → `{rule.get('replace')}`"
    if rtype == "AST_PARAM_RENAME":
        return (
            f"`{rule.get('old_param')}` → `{rule.get('new_param')}` "
            f"({rule.get('function_target')})"
        )
    if rtype == "AST_PARAM_DROP":
        param = rule.get("param") or rule.get("old_param")
        vals = rule.get("values")
        suffix = f" values={vals}" if vals else ""
        return f"drop `{param}`{suffix} ({rule.get('function_target')})"
    if rtype == "DEPENDENCY_BUMP":
        return (
            f"`{rule.get('package')}` "
            f"{rule.get('from_version')} → {rule.get('to_version')}"
        )
    if rtype == "DEPENDENCY_ADD":
        scope = str(rule.get("scope") or "main")
        return f"add `{rule.get('package')}` {rule.get('to_version')} ({scope})"
    if rtype == "DEPENDENCY_REMOVE":
        scope = str(rule.get("scope") or "main")
        return f"remove `{rule.get('package')}` ({scope})"
    if rtype == "AST_IMPORT_REWRITE":
        return f"import `{rule.get('old_import')}` → `{rule.get('new_import')}`"
    if rtype == "AST_ATTR_RENAME":
        return f"attr `{rule.get('old_attr')}` → `{rule.get('new_attr')}`"
    if rtype == "AST_CALL_REWRITE":
        return f"call `{rule.get('old_callee')}` → `{rule.get('new_callee')}`"
    if rtype == "KEY_RENAME":
        return f"key `{rule.get('old_key')}` → `{rule.get('new_key')}`"
    return rtype or "(rule)"


def _rationale_lines(packet: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        summary = _rule_summary(rule)
        reason = str(rule.get("reason") or "").strip()
        if reason:
            lines.append(f"- {summary}: {reason}")
        else:
            lines.append(f"- {summary}")
    return lines


def _source_lines(packet: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for src in packet.get("sources") or []:
        if not isinstance(src, dict):
            continue
        url = str(src.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        kind = str(src.get("kind") or "other")
        lines.append(f"- ({kind}) {url}")
    return lines


def build_pr_body(
    packet: dict[str, Any],
    report: PatchReport,
    test_result: TestResult,
    *,
    detect_summary: str = "",
    review_markdown: str = "",
) -> str:
    package = packet.get("package", "package")
    from_v = packet.get("from_version", "?")
    to_v = packet.get("to_version", "?")
    packet_id = packet.get("packet_id", "")

    change_lines = [f"- {c.detail} in `{c.path}`" for c in report.changes] or [
        "- (no file-level change details recorded)"
    ]
    status = "Passed" if test_result.passed else "Failed"
    detect_block = f"\n### Detect\n{detect_summary}\n" if detect_summary else "\n"

    rationale = _rationale_lines(packet)
    rationale_block = (
        "\n### Rationale\n" + "\n".join(rationale) + "\n"
        if rationale
        else "\n"
    )

    notes = str(packet.get("notes") or "").strip()
    notes_block = f"\n### Notes\n{notes}\n" if notes else "\n"

    sources = _source_lines(packet)
    sources_block = (
        "\n### Sources\n" + "\n".join(sources) + "\n"
        if sources
        else "\n"
    )

    review_block = f"\n{review_markdown}\n" if review_markdown.strip() else "\n"

    return f"""## Conduit migration

**Package:** `{package}` `{from_v}` → `{to_v}`
**Packet:** `{packet_id}`
{detect_block}
### Changes applied
{chr(10).join(change_lines)}
{rationale_block}{notes_block}{sources_block}{review_block}
### Verification
- Tests: {status}
- Command: `{' '.join(test_result.command) or 'n/a'}`

---
*Generated by Conduit*
"""


def build_pr_title(packet: dict[str, Any]) -> str:
    package = packet.get("package", "package")
    to_v = packet.get("to_version", "new")
    return f"[Conduit] Upgrade {package} to {to_v}"


def open_pull_request(
    root: Path,
    packet: dict[str, Any],
    report: PatchReport,
    test_result: TestResult,
    *,
    push: bool = True,
    create_pr: bool = True,
    detect_summary: str = "",
    review_markdown: str = "",
) -> PRResult:
    package = str(packet.get("package", "package"))
    to_v = str(packet.get("to_version", "version")).replace("/", "-")
    branch = f"conduit/upgrade-{package}-{to_v}"
    title = build_pr_title(packet)
    body = build_pr_body(
        packet,
        report,
        test_result,
        detect_summary=detect_summary,
        review_markdown=review_markdown,
    )

    if _run(["git", "rev-parse", "--is-inside-work-tree"], root).returncode != 0:
        return PRResult(
            branch=branch,
            title=title,
            url=None,
            created=False,
            message="Not a git repository; skipped PR creation.",
        )

    _run(["git", "checkout", "-B", branch], root)
    if report.files_modified:
        _run(["git", "add", "--"] + report.files_modified, root)
    else:
        _run(["git", "add", "-A"], root)

    commit = _run(["git", "commit", "-m", title], root)
    if commit.returncode != 0 and "nothing to commit" not in (
        commit.stdout + commit.stderr
    ):
        pass

    if push:
        push_proc = _run(["git", "push", "-u", "origin", "HEAD"], root)
        if push_proc.returncode != 0:
            return PRResult(
                branch=branch,
                title=title,
                url=None,
                created=False,
                message=f"Push failed: {push_proc.stderr.strip() or push_proc.stdout.strip()}",
            )

    if not create_pr:
        return PRResult(
            branch=branch,
            title=title,
            url=None,
            created=True,
            message=f"Branch {branch} ready (PR creation skipped).",
        )

    gh = _resolve_gh()
    if not gh:
        return PRResult(
            branch=branch,
            title=title,
            url=None,
            created=False,
            message=GH_MISSING_MSG,
        )

    pr = _run(
        [gh, "pr", "create", "--title", title, "--body", body, "--head", branch],
        root,
    )
    if pr.returncode != 0:
        return PRResult(
            branch=branch,
            title=title,
            url=None,
            created=False,
            message=f"gh pr create failed: {pr.stderr.strip() or pr.stdout.strip()}",
        )

    url = (pr.stdout or "").strip().splitlines()[-1] if pr.stdout else None
    return PRResult(
        branch=branch,
        title=title,
        url=url,
        created=True,
        message=f"Opened PR: {url}",
    )
