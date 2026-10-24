"""Baseline-aware test runs: tests already failing on the base state are not blamed on a fix."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, ApplyReport, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.report import format_report
from conduit.apply.testrun import (
    Baseline,
    TestRun,
    describe_preexisting,
    focus,
    judge,
    parse_junit,
    parse_summary,
    record_baseline,
    run_tests,
)
from conduit.main import app
from test_apply import _edit, _impact_payload, _settings

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"

JUNIT = """<?xml version="1.0"?><testsuites><testsuite>
<testcase classname="" name="checks.test_b" file="checks/test_b.py"><error message="collection failure">ImportError</error></testcase>
<testcase classname="checks.test_a.TestK" name="test_ok" file="checks/test_a.py" line="2" />
<testcase classname="checks.test_a.TestK" name="test_bad" file="checks/test_a.py" line="3"><failure message="assert 0">checks/test_a.py:4: AssertionError</failure></testcase>
<testcase classname="checks.test_a" name="test_p[a::b]" file="checks/test_a.py" line="4"><failure message="m">x</failure></testcase>
<testcase classname="checks.test_a" name="test_skip" file="checks/test_a.py" line="6"><skipped message="no" /></testcase>
<testcase classname="checks.test_a" name="test_teardown" file="checks/test_a.py" line="8"><failure message="f">call</failure></testcase>
<testcase classname="checks.test_a" name="test_teardown" file="checks/test_a.py" line="8"><error message="e">teardown</error></testcase>
</testsuite></testsuites>"""


def _run(outcomes: dict[str, str], *, exit_code: int = 1, source: str = "junit", details=None) -> TestRun:
    return TestRun(exit_code=exit_code, output="full output", outcomes=outcomes, details=details or {}, source=source)


def _baseline(outcomes: dict[str, str], source: str = "junit") -> Baseline:
    return Baseline(command="cmd", exit_code=1, source=source, outcomes=outcomes)


def test_junit_cases_get_the_ids_pytest_prints():
    outcomes, details = parse_junit(JUNIT)
    assert outcomes == {
        "checks/test_b.py": "error",
        "checks/test_a.py::TestK::test_ok": "passed",
        "checks/test_a.py::TestK::test_bad": "failed",
        "checks/test_a.py::test_p[a::b]": "failed",
        "checks/test_a.py::test_skip": "skipped",
        "checks/test_a.py::test_teardown": "error",
    }
    assert "AssertionError" in details["checks/test_a.py::TestK::test_bad"]
    assert "teardown" in details["checks/test_a.py::test_teardown"]


def test_summary_lines_name_failures_when_there_is_no_report():
    output = "FAILED                                   [ 85%]\nFAILED a/test_x.py::T::t - boom\nERROR a/test_y.py\nERROR something else\n"
    assert parse_summary(output) == {"a/test_x.py::T::t": "failed", "a/test_y.py": "error"}


def test_failure_that_also_failed_on_base_is_preexisting_not_new():
    base = _baseline({"t.py::old": "failed", "t.py::fine": "passed", "t.py::flaky": "error"})
    verdict = judge(_run({"t.py::old": "failed", "t.py::fine": "passed", "t.py::flaky": "failed"}), base)
    assert verdict.passed and verdict.new == [] and verdict.preexisting == ["t.py::flaky", "t.py::old"]


def test_failure_of_a_test_that_passed_on_base_is_new():
    base = _baseline({"t.py::old": "failed", "t.py::fine": "passed"})
    verdict = judge(_run({"t.py::old": "failed", "t.py::fine": "failed"}), base)
    assert not verdict.passed and verdict.new == ["t.py::fine"] and verdict.preexisting == ["t.py::old"]


def test_test_absent_from_base_that_fails_is_new():
    verdict = judge(_run({"t.py::added": "failed"}), _baseline({"t.py::old": "failed"}))
    assert not verdict.passed and verdict.new == ["t.py::added"]


def test_test_that_passed_on_base_and_no_longer_runs_counts_against_the_fix():
    base = _baseline({"t.py::a": "passed", "t.py::b": "passed", "t.py::old": "failed"})
    verdict = judge(_run({"t.py::a": "passed", "t.py::old": "failed"}), base)
    assert not verdict.passed and verdict.missing == ["t.py::b"]


def test_without_per_test_results_the_exit_code_decides():
    base = _baseline({"t.py::old": "failed"})
    assert not judge(_run({}, source="exit-code"), base).passed
    assert not judge(_run({"t.py::old": "failed"}), _baseline({}, source="exit-code")).passed
    assert not judge(_run({"t.py::old": "failed"}), None).passed
    assert judge(_run({}, exit_code=0, source="exit-code"), base).passed


def test_feedback_holds_only_the_new_failures():
    run = _run({"t.py::old": "failed", "t.py::fine": "failed"}, details={"t.py::old": "OLD TRACE", "t.py::fine": "NEW TRACE"})
    verdict = judge(run, _baseline({"t.py::old": "failed", "t.py::fine": "passed"}))
    text = focus(run, verdict)
    assert "NEW TRACE" in text and "OLD TRACE" not in text
    # Nothing pre-existing: the whole output, as before.
    lone = _run({"t.py::fine": "failed"})
    assert focus(lone, judge(lone, _baseline({"t.py::fine": "passed"}))) == "full output"


def test_note_names_the_preexisting_failures():
    assert describe_preexisting([]) == ""
    assert describe_preexisting(["a", "b"]) == "2 tests already failing on base, not caused by Conduit: a, b"
    assert describe_preexisting(["a"]).startswith("1 test already")


def test_report_json_is_unchanged_without_a_baseline():
    assert set(ApplyReport().to_dict()) == {"results", "tokens_used", "summary"}
    data = ApplyReport(preexisting=["t.py::old"], new_failures=[]).to_dict()
    assert data["preexisting_failures"] == ["t.py::old"] and data["new_failures"] == []
    assert data["preexisting_note"] == "1 test already failing on base, not caused by Conduit: t.py::old"


def _checks(root: Path, body: str) -> None:
    (root / "checks").mkdir(exist_ok=True)
    (root / "checks" / "test_it.py").write_text(textwrap.dedent(body), encoding="utf-8")


def test_run_records_each_pytest_outcome_and_other_runners_fall_back(tmp_path: Path):
    _checks(tmp_path, """\
        def test_ok():
            pass

        def test_bad():
            assert 0, "BAD"
        """)
    run = run_tests(PYTEST, tmp_path)
    assert run.source == "junit" and run.exit_code == 1
    assert run.outcomes == {"checks/test_it.py::test_ok": "passed", "checks/test_it.py::test_bad": "failed"}
    assert "BAD" in run.details["checks/test_it.py::test_bad"]
    other = run_tests(f"{sys.executable} -c \"raise SystemExit(3)\"", tmp_path)
    assert other.source == "exit-code" and other.outcomes == {} and not other.ok


def test_a_module_that_fails_to_import_does_not_stop_the_rest(tmp_path: Path):
    _checks(tmp_path, "def test_ok():\n    pass\n")
    (tmp_path / "checks" / "test_broken.py").write_text("import not_a_module_anywhere\n", encoding="utf-8")
    run = run_tests(PYTEST, tmp_path)
    assert run.outcomes == {"checks/test_broken.py": "error", "checks/test_it.py::test_ok": "passed"}


def test_baseline_at_a_ref_runs_the_base_code(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _checks(root, "def test_flag():\n    assert open('flag.txt').read() == 'base'\n")
    (root / "flag.txt").write_text("base", encoding="utf-8")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "base"], check=True)
    (root / "flag.txt").write_text("changed", encoding="utf-8")
    assert record_baseline(PYTEST, root).outcomes == {"checks/test_it.py::test_flag": "failed"}
    at_ref = record_baseline(PYTEST, root, "HEAD")
    assert at_ref.outcomes == {"checks/test_it.py::test_flag": "passed"} and at_ref.ref == "HEAD"
    assert (root / "flag.txt").read_text() == "changed"  # the tree is not touched


def test_baseline_command_writes_json(tmp_path: Path):
    _checks(tmp_path, "def test_old():\n    assert 0\n")
    out = tmp_path / "baseline.json"
    result = CliRunner().invoke(app, ["baseline", "--path", str(tmp_path), "--test-cmd", PYTEST, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "already failing: checks/test_it.py::test_old" in result.output
    data = json.loads(out.read_text())
    assert data["failing"] == ["checks/test_it.py::test_old"] and data["source"] == "junit"
    assert Baseline.load(out).failing == {"checks/test_it.py::test_old"}


def _repo(tmp_path: Path) -> tuple[Path, list]:
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text("def broken(client):\n    return client.old_call(1)\n", encoding="utf-8")
    (root / "two.py").write_text("def second(client):\n    return client.old_call(2)\n", encoding="utf-8")
    _checks(root, """\
        import mod, two

        class Client:
            def old_call(self, n):
                return n

            def new_call(self, n):
                return n

        def test_already_broken():
            assert False, "PRE-EXISTING TRACE"

        def test_mod():
            assert mod.broken(Client()) == 1

        def test_two():
            assert two.second(Client()) == 2
        """)
    payload = _impact_payload(line=2)
    payload["impacts"][0]["sites"].append(dict(payload["impacts"][0]["sites"][0], path="two.py", line=2))
    return root, sites_from_payload(payload)


def _config(root: Path, client, baseline=None, **kw) -> ApplyConfig:
    return ApplyConfig(root=root, settings=_settings(**kw), write=True, test_cmd=PYTEST, client=client,
                       site_cleared=lambda *_a: True, baseline=baseline)


def _edits(second: str) -> list[str]:
    return [_edit("broken", "client.new_call(1)"), "why", _edit("second", second), "why"]


def test_preexisting_failure_does_not_block_or_revert_the_fixes(tmp_path: Path):
    root, sites = _repo(tmp_path)
    baseline = record_baseline(PYTEST, root)
    assert baseline.failing == {"checks/test_it.py::test_already_broken"}
    report = apply_sites(sites, _config(root, FakeModelClient(responses=_edits("client.new_call(2)")), baseline))
    assert [r.status for r in report.results] == [SiteStatus.FIXED_VERIFIED, SiteStatus.FIXED_VERIFIED]
    assert report.preexisting == ["checks/test_it.py::test_already_broken"] and report.new_failures == []
    assert "1 test already failing on base, not caused by Conduit: checks/test_it.py::test_already_broken" in format_report(report)
    assert "new_call" in (root / "two.py").read_text()


def test_without_a_baseline_the_same_failure_reverts_everything(tmp_path: Path):
    root, sites = _repo(tmp_path)
    report = apply_sites(sites, _config(root, FakeModelClient(responses=_edits("client.new_call(2)") * 3)))
    assert all(r.status == SiteStatus.NEEDS_HUMAN for r in report.results)
    assert "preexisting_failures" not in report.to_dict()


def test_new_failure_is_blamed_retried_and_reverted_with_only_its_own_trace(tmp_path: Path):
    root, sites = _repo(tmp_path)
    baseline = record_baseline(PYTEST, root)
    client = FakeModelClient(responses=_edits("client.bad_call(2)") + [_edit("second", "client.bad_call(3)"), "why"])
    report = apply_sites(sites, _config(root, client, baseline, max_attempts=2))
    first, second = report.results
    assert first.status == SiteStatus.FIXED_VERIFIED, (first.error, first.why, len(client.calls))
    assert second.status == SiteStatus.NEEDS_HUMAN and "reverted" in second.error
    retry = client.calls[4]["messages"][1].content
    assert "bad_call" in retry and "checks/test_it.py::test_two" in retry and "PRE-EXISTING TRACE" not in retry
    assert report.preexisting == ["checks/test_it.py::test_already_broken"]
    assert "new_call(1)" in (root / "mod.py").read_text() and "old_call(2)" in (root / "two.py").read_text()
