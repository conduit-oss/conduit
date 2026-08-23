"""Detect and run the project's native test suite."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


_COUNT_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|skipped|error|errors)\b",
    re.I,
)
_NO_TESTS_RE = re.compile(r"\bno tests ran\b|\bcollected 0 items\b", re.I)
_MISSING_KEY_RE = re.compile(
    r"OPENAI_API_KEY(?:\s+\(or OPENAI_KEY\))?\s+is not set",
    re.I,
)
_NPM_PASS_RE = re.compile(
    r"(?:Tests|Test Suites):\s+(?P<passed>\d+)\s+passed",
    re.I,
)


@dataclass
class TestResult:
    runner: str
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    command: list[str]
    passed_count: int | None = None
    failed_count: int | None = None
    skipped_count: int | None = None
    error_count: int | None = None
    fail_reason: str = ""
    extra_notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.fail_reason and not self.passed:
            cmd = " ".join(self.command) if self.command else self.runner
            return f"Failed: {self.fail_reason} via {cmd} (exit {self.returncode})"
        status = "Passed" if self.passed else "Failed"
        cmd = " ".join(self.command) if self.command else self.runner or "tests"
        return f"{status} via {cmd} (exit {self.returncode})"


TestResult.__test__ = False  # type: ignore[attr-defined]


def parse_pytest_counts(text: str) -> dict[str, int]:
    """Parse pytest summary counts from stdout+stderr."""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    found = False
    for match in _COUNT_RE.finditer(text or ""):
        found = True
        kind = match.group("kind").lower()
        n = int(match.group("count"))
        if kind in {"error", "errors"}:
            counts["error"] += n
        else:
            counts[kind] = n
    if not found:
        return counts
    return counts


def evaluate_pytest_result(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> tuple[bool, str, dict[str, int]]:
    """Decide pass/fail from pytest output. Skip-all is a failure."""
    blob = f"{stdout or ''}\n{stderr or ''}"
    counts = parse_pytest_counts(blob)
    reason = ""
    if _MISSING_KEY_RE.search(blob):
        reason = "OPENAI_API_KEY is not set (tests skipped)"
        return False, reason, counts
    if _NO_TESTS_RE.search(blob) and counts["passed"] == 0:
        return False, "no tests ran", counts
    if returncode != 0:
        if counts["failed"] or counts["error"]:
            return False, "", counts
        return False, "", counts
    # returncode 0
    if counts["error"] > 0 or counts["failed"] > 0:
        return False, "", counts
    if counts["passed"] > 0:
        return True, "", counts
    if counts["skipped"] > 0 and counts["passed"] == 0:
        return False, "all tests skipped", counts
    # Could not parse counts; do not treat silent skip as success.
    if "skipped" in blob.lower() and "passed" not in blob.lower():
        return False, "all tests skipped", counts
    if returncode == 0 and not any(counts.values()):
        # e.g. empty -q with no summary — still require evidence of a pass
        return False, "no passing tests reported", counts
    return True, "", counts


def evaluate_npm_result(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> tuple[bool, str]:
    blob = f"{stdout or ''}\n{stderr or ''}"
    if returncode != 0:
        return False, ""
    match = _NPM_PASS_RE.search(blob)
    if match and int(match.group("passed")) == 0:
        return False, "no passing tests reported"
    if re.search(r"\b0 passing\b", blob, re.I) and not re.search(
        r"[1-9]\d*\s+passing", blob, re.I
    ):
        return False, "no passing tests reported"
    return True, ""


def detect_test_command(root: Path) -> tuple[str, list[str]] | None:
    # Always use the interpreter running Conduit — not PATH `python`, which on
    # Windows often resolves to a different install (e.g. Store Python) that
    # still has the pre-migration package version.
    pytest_cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return "pytest", list(pytest_cmd)
    if (root / "pyproject.toml").exists():
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        if "[tool.pytest" in text or "pytest" in text:
            return "pytest", list(pytest_cmd)
    tests_dir = root / "tests"
    if tests_dir.is_dir() and any(tests_dir.rglob("test_*.py")):
        return "pytest", list(pytest_cmd)

    package_json = root / "package.json"
    if package_json.is_dir() is False and package_json.is_file():
        try:
            import json

            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            if "test" in scripts:
                npm = "npm.cmd" if shutil.which("npm.cmd") else "npm"
                return "npm", [npm, "test", "--silent"]
        except Exception:
            pass

    if (root / "go.mod").is_file():
        return "go", ["go", "test", "./..."]

    return None


def _from_proc(
    runner: str,
    command: list[str],
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> TestResult:
    fail_reason = ""
    passed = returncode == 0
    passed_count = failed_count = skipped_count = error_count = None
    if runner == "pytest":
        passed, fail_reason, counts = evaluate_pytest_result(
            returncode=returncode, stdout=stdout, stderr=stderr
        )
        passed_count = counts["passed"]
        failed_count = counts["failed"]
        skipped_count = counts["skipped"]
        error_count = counts["error"]
    elif runner == "npm":
        passed, fail_reason = evaluate_npm_result(
            returncode=returncode, stdout=stdout, stderr=stderr
        )
    else:
        passed = returncode == 0
    return TestResult(
        runner=runner,
        passed=passed,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        command=command,
        passed_count=passed_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        error_count=error_count,
        fail_reason=fail_reason,
    )


def run_tests(
    root: Path,
    *,
    timeout: float = 300.0,
    nodeids: list[str] | None = None,
) -> TestResult:
    detected = detect_test_command(root)
    if detected is None:
        return TestResult(
            runner="none",
            passed=False,
            returncode=1,
            stdout="No test suite detected.",
            stderr="",
            command=[],
            fail_reason="no test suite detected",
        )

    runner, command = detected
    command = list(command)
    if nodeids and runner == "pytest":
        for node in nodeids:
            n = str(node).strip()
            if n:
                command.append(n)

    try:
        proc = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return TestResult(
            runner=runner,
            passed=False,
            returncode=127,
            stdout="",
            stderr=str(exc),
            command=command,
            fail_reason=str(exc),
        )
    except subprocess.TimeoutExpired as exc:
        return TestResult(
            runner=runner,
            passed=False,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "timed out",
            command=command,
            fail_reason="timed out",
        )

    return _from_proc(
        runner,
        command,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
