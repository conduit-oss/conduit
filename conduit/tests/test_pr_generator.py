"""PR generator: missing git/gh binaries must not crash."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conduit.patcher.engine import PatchReport
from conduit.pr_generator import (
    GH_MISSING_MSG,
    _resolve_gh,
    _run,
    open_pull_request,
)
from conduit.test_runner import TestResult as RunnerResult


def _passed_tests() -> RunnerResult:
    return RunnerResult(
        runner="pytest",
        passed=True,
        returncode=0,
        stdout="",
        stderr="",
        command=["pytest", "-q"],
    )


def test_run_missing_binary_returns_127(tmp_path: Path):
    proc = _run(["definitely-not-a-real-cmd-xyz"], tmp_path)
    assert proc.returncode == 127
    assert "not found" in proc.stderr.lower()


def test_open_pull_request_missing_gh(monkeypatch, tmp_path: Path):
    def fake_run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="true\n", stderr=""
        )

    monkeypatch.setattr("conduit.pr_generator._run", fake_run)
    monkeypatch.setattr("conduit.pr_generator._resolve_gh", lambda: None)

    result = open_pull_request(
        tmp_path,
        {"package": "openai", "to_version": "1.0.0"},
        PatchReport(),
        _passed_tests(),
        push=False,
    )
    assert result.created is False
    assert result.url is None
    assert result.message == GH_MISSING_MSG
    assert "gh not found; install it or use --skip-pr" in result.message
    assert "https://cli.github.com/" in result.message
    assert "gh --version in the same terminal you use for Conduit" in result.message


def test_resolve_gh_prefers_exe_on_windows(monkeypatch):
    monkeypatch.setattr("conduit.pr_generator.sys.platform", "win32")
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        calls.append(name)
        if name == "gh.exe":
            return r"C:\Program Files\GitHub CLI\gh.exe"
        return None

    monkeypatch.setattr("conduit.pr_generator.shutil.which", fake_which)
    assert _resolve_gh() == r"C:\Program Files\GitHub CLI\gh.exe"
    assert calls == ["gh.exe"]
