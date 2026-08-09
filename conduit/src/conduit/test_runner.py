"""Detect and run the project's native test suite."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestResult:
    runner: str
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    command: list[str]

    @property
    def summary(self) -> str:
        status = "Passed" if self.passed else "Failed"
        return f"{status} via {' '.join(self.command)} (exit {self.returncode})"


def detect_test_command(root: Path) -> tuple[str, list[str]] | None:
    # Prefer `python -m pytest` so the active interpreter/venv is used.
    pytest_cmd = ["python", "-m", "pytest", "-q"]
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return "pytest", list(pytest_cmd)
    if (root / "pyproject.toml").exists():
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        if "[tool.pytest" in text or "pytest" in text:
            return "pytest", list(pytest_cmd)
    # Look for tests/ with python files
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


def run_tests(root: Path, *, timeout: float = 300.0) -> TestResult:
    detected = detect_test_command(root)
    if detected is None:
        return TestResult(
            runner="none",
            passed=True,
            returncode=0,
            stdout="No test suite detected; treating as pass.",
            stderr="",
            command=[],
        )

    runner, command = detected

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
        )
    except subprocess.TimeoutExpired as exc:
        return TestResult(
            runner=runner,
            passed=False,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "timed out",
            command=command,
        )

    return TestResult(
        runner=runner,
        passed=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        command=command,
    )
