"""Per-test outcomes of the repo's test command, and a baseline to compare them with.

The test command is run as given. For pytest, ``PYTEST_ADDOPTS`` adds a JUnit
XML report (and keeps going past a module that fails to import), so every test
gets an outcome; other runners ignore the variable. When no report is written,
the ``FAILED``/``ERROR`` lines of the output are used; when there are none of
those either, only the exit code is known and every failure counts.

A baseline is the same command on the PR's base state: the old dependency
release installed and the code unchanged. A test that also failed there is
pre-existing; it is reported, never blamed or reverted. Only a test that passed
there (or did not exist) and fails now is a new failure. Nothing here knows any
package, test or runner name beyond pytest's own report formats.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FAILING = ("failed", "error")
_SUMMARY = re.compile(r"^(FAILED|ERROR) (\S+)(?: - .*)?$", re.M)
_SCRATCH_IGNORE = (".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules")


@dataclass
class TestRun:
    """One run of the test command."""

    __test__ = False  # not a pytest class

    exit_code: int
    output: str
    #: test id -> "passed" | "failed" | "error" | "skipped"; empty when unknown.
    outcomes: dict[str, str] = field(default_factory=dict)
    #: test id -> the failure text the runner recorded (traceback).
    details: dict[str, str] = field(default_factory=dict)
    #: "junit", "output" (summary lines only), or "exit-code".
    source: str = "exit-code"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def failing(self) -> list[str]:
        return sorted(test for test, outcome in self.outcomes.items() if outcome in FAILING)

    def to_dict(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "source": self.source, "outcomes": dict(sorted(self.outcomes.items()))}


@dataclass
class Baseline:
    command: str
    exit_code: int
    source: str
    outcomes: dict[str, str]
    ref: str | None = None

    @property
    def usable(self) -> bool:
        """Per-test results exist (a bare exit code says nothing about which test)."""
        return self.source != "exit-code" and bool(self.outcomes)

    @property
    def failing(self) -> set[str]:
        return {test for test, outcome in self.outcomes.items() if outcome in FAILING}

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "ref": self.ref,
            "exit_code": self.exit_code,
            "source": self.source,
            "tests": len(self.outcomes),
            "failing": sorted(self.failing),
            "outcomes": dict(sorted(self.outcomes.items())),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Baseline":
        return cls(
            command=str(data.get("command") or ""),
            exit_code=int(data.get("exit_code") or 0),
            source=str(data.get("source") or "exit-code"),
            outcomes={str(k): str(v) for k, v in (data.get("outcomes") or {}).items()},
            ref=data.get("ref"),
        )

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


@dataclass
class Verdict:
    """A run judged against the baseline."""

    passed: bool
    new: list[str]
    preexisting: list[str]
    #: Tests that passed on the baseline and have no outcome now.
    missing: list[str]


def _node_id(case: ET.Element) -> str:
    file = (case.get("file") or "").replace("\\", "/")
    classname = case.get("classname") or ""
    name = case.get("name") or ""
    if not classname:
        return file or name.replace(".", "/") + ".py"
    if file.endswith(".py"):
        module = file[:-3].replace("/", ".")
        if classname == module:
            return f"{file}::{name}"
        if classname.startswith(module + "."):
            return f"{file}::{classname[len(module) + 1:].replace('.', '::')}::{name}"
    return f"{classname}::{name}"


def parse_junit(text: str) -> tuple[dict[str, str], dict[str, str]]:
    outcomes: dict[str, str] = {}
    details: dict[str, str] = {}
    root = ET.fromstring(text)
    for case in root.iter("testcase"):
        test = _node_id(case)
        outcome = "passed"
        for child in case:
            tag = child.tag
            if tag in ("failure", "error"):
                outcome = "failed" if tag == "failure" and outcome != "error" else "error"
                details[test] = (details.get(test, "") + "\n" + (child.get("message") or "") + "\n" + (child.text or "")).strip()
            elif tag == "skipped" and outcome == "passed":
                outcome = "skipped"
        if outcomes.get(test) in FAILING and outcome not in FAILING:
            continue  # a rerun or teardown entry cannot clear a failure
        outcomes[test] = outcome
    return outcomes, details


def parse_summary(output: str) -> dict[str, str]:
    """``FAILED <id>`` / ``ERROR <id>`` lines of pytest's short summary."""
    found: dict[str, str] = {}
    for match in _SUMMARY.finditer(output or ""):
        test = match.group(2)
        if "::" in test or test.endswith(".py"):
            found[test] = "failed" if match.group(1) == "FAILED" else "error"
    return found


def run_tests(command: str, cwd: Path, env: dict[str, str] | None = None) -> TestRun:
    """``env`` (default: this process's) is the environment the command runs in."""
    with tempfile.TemporaryDirectory(prefix="conduit-junit-") as tmp:
        report = Path(tmp) / "junit.xml"
        env = dict(os.environ if env is None else env)
        extra = f"--junitxml={report} -o junit_family=xunit1 --continue-on-collection-errors"
        env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") + " " + extra).strip()
        # A file put back within the same second at the same size would load a
        # stale .pyc of the rewrite: keep bytecode out of the scratch tree.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, check=False, env=env)
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        outcomes: dict[str, str] = {}
        details: dict[str, str] = {}
        source = "exit-code"
        if report.is_file():
            try:
                outcomes, details = parse_junit(report.read_text(encoding="utf-8"))
                source = "junit"
            except ET.ParseError:
                outcomes, details = {}, {}
    # A second runner in the same command (or one that ignores the report)
    # still prints its summary: count those failures too.
    for test, outcome in parse_summary(output).items():
        if outcomes.get(test) not in FAILING:
            outcomes[test] = outcome
            if source == "exit-code":
                source = "output"
    if proc.returncode != 0 and not any(o in FAILING for o in outcomes.values()):
        # The command failed without naming a test: nothing per test can be trusted.
        source = "exit-code"
    return TestRun(exit_code=proc.returncode, output=output, outcomes=outcomes, details=details, source=source)


_OPTION_TAKES_VALUE = {"-p", "-o", "-k", "-m", "-c", "-W", "--override-ini", "-n"}


def isolate_pytest(command: str, node: str) -> str | None:
    """The same pytest command, running only ``node``.

    ``None`` when ``command`` is not pytest or ``node`` is not a test id, so a
    runner this cannot split is left to the whole-suite comparison."""
    if "pytest" not in command or ("::" not in node and not node.endswith(".py")):
        return None
    parts = shlex.split(command)
    prefix: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part.startswith("-"):
            prefix.append(part)
            if part in _OPTION_TAKES_VALUE and index + 1 < len(parts) and not parts[index + 1].startswith("-"):
                index += 1
                prefix.append(parts[index])
            index += 1
            continue
        base = part.rsplit("/", 1)[-1]
        if base.startswith("python") or part == "-m" or base == "pytest" or part.endswith("pytest"):
            prefix.append(part)
            index += 1
            continue
        break
    if not any("pytest" in part for part in prefix):
        return None
    return shlex.join([*prefix, node])


def judge(run: TestRun, baseline: Baseline | None) -> Verdict:
    """Passed when nothing fails that did not already fail on the baseline."""
    if run.ok and not run.failing:
        return Verdict(True, [], [], [])
    if baseline is None or not baseline.usable or run.source == "exit-code":
        return Verdict(run.ok, run.failing or ([] if run.ok else ["(test command)"]), [], [])
    before = baseline.failing
    preexisting = [test for test in run.failing if test in before]
    new = [test for test in run.failing if test not in before]
    missing: list[str] = []
    if run.source == "junit":
        missing = sorted(
            test for test, outcome in baseline.outcomes.items() if outcome == "passed" and test not in run.outcomes
        )
    return Verdict(not new and not missing, new, preexisting, missing)


def focus(run: TestRun, verdict: Verdict) -> str:
    """The failure text for blame and model feedback, without pre-existing failures."""
    if not verdict.preexisting:
        return run.output
    parts: list[str] = []
    for test in verdict.new:
        parts.append(f"FAILED {test}\n{run.details.get(test, '')}".rstrip())
    if verdict.missing:
        shown = ", ".join(verdict.missing[:10]) + (" ..." if len(verdict.missing) > 10 else "")
        parts.append(f"{len(verdict.missing)} tests that passed on the base did not run: {shown}")
    return "\n\n".join(parts) or run.output


def describe_preexisting(tests: list[str]) -> str:
    if not tests:
        return ""
    noun = "test" if len(tests) == 1 else "tests"
    return f"{len(tests)} {noun} already failing on base, not caused by Conduit: " + ", ".join(tests)


def base_tree(root: Path, ref: str, dest: Path) -> Path:
    """The repo's files at ``ref`` (git archive), so the baseline sees the base code."""
    archive = dest / "base.tar"
    with archive.open("wb") as fh:
        subprocess.run(["git", "-C", str(root), "archive", "--format=tar", ref], stdout=fh, check=True)
    tree = dest / "tree"
    tree.mkdir()
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(tree, filter="data")
        else:  # Python < 3.11.4: git archive output has no links outside the tree
            tar.extractall(tree)
    archive.unlink()
    return tree


def record_baseline(command: str, root: Path, ref: str | None = None) -> Baseline:
    """Run ``command`` on the base state, in a scratch copy so nothing in ``root`` changes."""
    with tempfile.TemporaryDirectory(prefix="conduit-baseline-") as tmp:
        if ref:
            tree = base_tree(root, ref, Path(tmp))
        else:
            tree = Path(tmp) / "tree"
            shutil.copytree(root, tree, ignore=shutil.ignore_patterns(*_SCRATCH_IGNORE))
        run = run_tests(command, tree)
    return Baseline(command=command, exit_code=run.exit_code, source=run.source, outcomes=run.outcomes, ref=ref)
