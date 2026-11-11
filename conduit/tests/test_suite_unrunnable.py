"""Suite-could-not-run status: never verified when tests did not actually run.

Made-up packages only. Covers collection/import failures, interpreter crashes,
timeouts, and no-tests-collected. Also covers partial-out checkpoints.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.testrun import TestRun, judge, record_baseline, suite_couldnt_run
from test_apply import _edit, _impact_payload, _settings

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _sites(*specs: tuple[str, int, str]) -> list:
    payload = _impact_payload(path=specs[0][0], line=specs[0][1])
    payload["impacts"][0]["sites"] = [
        {"path": path, "line": line, "expr": expr, "confidence": "high", "via_wrapper": False}
        for path, line, expr in specs
    ]
    return sites_from_payload(payload)


def _client(root: Path) -> None:
    _write(
        root / "support.py",
        """\
        class Client:
            def old_call(self, n):
                return n

            def new_call(self, n):
                return n
        """,
    )


def test_suite_couldnt_run_detects_collection_crash_timeout_and_empty():
    assert suite_couldnt_run(output="ERROR collecting checks/test_x.py\nImportError: boom")
    assert suite_couldnt_run(TestRun(exit_code=2, output="usage error", outcomes={}, source="exit-code"))
    assert suite_couldnt_run(TestRun(exit_code=5, output="no tests collected", outcomes={}, source="exit-code"))
    assert suite_couldnt_run(output="Fatal Python error: Segmentation fault")
    assert suite_couldnt_run(output="TimeoutError: timed out")
    assert not suite_couldnt_run(TestRun(exit_code=0, output="1 passed", outcomes={"t": "passed"}, source="junit"))
    assert not suite_couldnt_run(TestRun(exit_code=1, output="FAILED t", outcomes={"t": "failed"}, source="junit"))


def test_judge_never_passes_when_suite_couldnt_run():
    run = TestRun(
        exit_code=2,
        output="ERROR collecting checks/test_x.py\nImportError",
        outcomes={"checks/test_x.py": "error", "checks/test_ok.py::test_a": "passed"},
        source="junit",
    )
    verdict = judge(run, None)
    assert not verdict.passed


def test_judge_never_passes_collection_error_with_usable_baseline():
    """Hole: continue-on-collection-errors left some tests green; baseline usable.

    Collection errors that only touch nodes already failing on the base used to
    make judge return passed=True. Verified status must never follow from that.
    """
    from conduit.apply.testrun import Baseline

    base = Baseline(
        command="cmd",
        exit_code=1,
        source="junit",
        outcomes={
            "checks/test_ok.py::test_a": "passed",
            "checks/test_pre.py::test_b": "failed",
        },
    )
    run = TestRun(
        exit_code=2,
        output="ERROR collecting checks/test_pre.py\nImportError: boom\n1 passed",
        outcomes={
            "checks/test_ok.py::test_a": "passed",
            "checks/test_pre.py::test_b": "error",
        },
        source="junit",
    )
    assert suite_couldnt_run(run)
    verdict = judge(run, base)
    assert not verdict.passed
    assert verdict.preexisting == ["checks/test_pre.py::test_b"]
    # Preexisting collection nodes must not be listed as new failures (that made
    # salvage treat bump-only breakage as rewrite-caused).
    assert verdict.new == ["(test command)"]
    assert "checks/test_pre.py::test_b" not in verdict.new


def test_collection_error_keeps_rewrite_but_never_verified(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "crate" / "__init__.py", "class Present:\n    pass\n")
    _write(root / "good.py", "def fine(client):\n    return client.old_call(1)\n")
    _client(root)
    _write(
        root / "checks" / "test_it.py",
        """\
        import good
        from support import Client

        def test_fine():
            assert good.fine(Client()) == 1
        """,
    )
    _write(root / "checks" / "test_broken.py", "from crate import Present\n\ndef test_x():\n    assert True\n")
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    # After the "bump", the symbol tests import is gone — collection breaks.
    _write(root / "crate" / "__init__.py", "class Other:\n    pass\n")
    client = FakeModelClient(responses=[_edit("fine", "client.new_call(1)"), "why"])
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_a: True,
            baseline=baseline,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_TESTS_COULDNT_RUN, (result.status, result.error, result.why)
    assert result.status != SiteStatus.FIXED_VERIFIED
    assert "new_call(1)" in (root / "good.py").read_text(encoding="utf-8")
    assert report.suite_blocked


def test_no_tests_collected_is_not_verified(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "good.py", "def fine(client):\n    return client.old_call(1)\n")
    _client(root)
    (root / "checks").mkdir(parents=True, exist_ok=True)
    # Empty checks dir → pytest exit 5, no tests collected.
    command = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"
    baseline = record_baseline(command, root)
    client = FakeModelClient(responses=[_edit("fine", "client.new_call(1)"), "why"])
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=command,
            client=client,
            site_cleared=lambda *_a: True,
            baseline=baseline if baseline.usable else None,
        ),
    )
    (result,) = report.results
    assert result.status != SiteStatus.FIXED_VERIFIED, (result.status, result.error, result.why)
    assert result.status in {
        SiteStatus.FIXED_TESTS_COULDNT_RUN,
        SiteStatus.NEEDS_HUMAN,
        SiteStatus.FIXED_NOT_COVERED,
    }


def test_scoped_site_ok_but_full_suite_collection_error_never_verified(tmp_path: Path):
    """Static per-site check passes; full post-apply suite has a collection error.

    Some other tests still pass under continue-on-collection-errors. The rewrite
    must stay as tests-couldn't-run (never verified).
    """
    root = tmp_path / "app"
    _client(root)
    _write(root / "good.py", "def fine(client):\n    return client.old_call(1)\n")
    _write(root / "crate" / "__init__.py", "class Present:\n    pass\n")
    _write(
        root / "checks" / "test_ok.py",
        """\
        import good
        from support import Client

        def test_fine():
            assert good.fine(Client()) == 1
        """,
    )
    # Already-failing test on the baseline (assertion). After the bump it fails
    # at collection instead — the hole that used to look like "only preexisting".
    _write(
        root / "checks" / "test_pre.py",
        """\
        def test_pre():
            assert False
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable
    assert any(outcome == "failed" for outcome in baseline.outcomes.values())
    # Bump: Present gone; test_pre now fails to import at collection.
    _write(root / "crate" / "__init__.py", "class Other:\n    pass\n")
    _write(
        root / "checks" / "test_pre.py",
        """\
        from crate import Present

        def test_pre():
            assert Present is not None
        """,
    )
    client = FakeModelClient(responses=[_edit("fine", "client.new_call(1)"), "why"])
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            # Scoped static recheck succeeds; the failure is only at full suite.
            site_cleared=lambda *_a: True,
            baseline=baseline,
        ),
    )
    (result,) = report.results
    assert "new_call(1)" in (root / "good.py").read_text(encoding="utf-8")
    assert result.status != SiteStatus.FIXED_VERIFIED, (result.status, result.error, result.why)
    assert result.status == SiteStatus.FIXED_TESTS_COULDNT_RUN, (result.status, result.error, result.why)
    assert report.suite_blocked


def test_collection_error_keeps_edits_never_verified_exit_friendly(tmp_path: Path):
    """Collection error after a7fbeb8 must keep compiling edits (not empty the diff).

    Made-up package only. The suite cannot collect because a bumped symbol is
    gone from tests; the rewrite itself is fine and must stay as
    FIXED_TESTS_COULDNT_RUN with a non-empty file change.
    """
    root = tmp_path / "app"
    _client(root)
    _write(root / "good.py", "def fine(client):\n    return client.old_call(1)\n")
    _write(root / "crate" / "__init__.py", "class Present:\n    pass\n")
    _write(
        root / "checks" / "test_ok.py",
        """\
        import good
        from support import Client

        def test_fine():
            assert good.fine(Client()) == 1
        """,
    )
    _write(
        root / "checks" / "test_broken.py",
        "from crate import Present\n\ndef test_x():\n    assert Present is not None\n",
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    _write(root / "crate" / "__init__.py", "class Other:\n    pass\n")
    client = FakeModelClient(responses=[_edit("fine", "client.new_call(1)"), "why"])
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=2),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_a: True,
            baseline=baseline,
        ),
    )
    (result,) = report.results
    text = (root / "good.py").read_text(encoding="utf-8")
    assert "new_call(1)" in text
    assert result.diff and "new_call" in result.diff
    assert result.status == SiteStatus.FIXED_TESTS_COULDNT_RUN, (result.status, result.error, result.why)
    assert result.status != SiteStatus.FIXED_VERIFIED
    assert report.suite_blocked
    # No model retry storm: one propose + one summarize.
    assert len(client.calls) == 2


def test_one_bad_site_reverted_while_good_site_kept(tmp_path: Path):
    """Assertion failure on one rewrite reverts only that site; the other stays."""
    root = tmp_path / "app"
    _client(root)
    _write(root / "good.py", "def fine(client):\n    return client.old_call(1)\n")
    _write(root / "bad.py", "def stuck(client):\n    return client.old_call(1)\n")
    _write(
        root / "checks" / "test_it.py",
        """\
        import good, bad
        from support import Client

        def test_fine():
            assert good.fine(Client()) == 1

        def test_stuck():
            assert bad.stuck(Client()) == 1
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    client = FakeModelClient(
        responses=[
            _edit("fine", "client.new_call(1)"),
            "why",
            _edit("stuck", "client.new_call(0)"),
            "why",
            _edit("stuck", "client.new_call(0)"),
            _edit("stuck", "client.new_call(0)"),
        ]
    )
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call"), ("bad.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=2),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_a: True,
            baseline=baseline,
        ),
    )
    by_path = {item.site.path: item for item in report.results}
    fine, stuck = by_path["good.py"], by_path["bad.py"]
    assert "new_call(1)" in (root / "good.py").read_text(encoding="utf-8")
    assert "old_call(1)" in (root / "bad.py").read_text(encoding="utf-8")
    assert fine.status == SiteStatus.FIXED_VERIFIED, (fine.status, fine.error, fine.why)
    assert stuck.status == SiteStatus.NEEDS_HUMAN
    assert "reverted" in (stuck.error or "")
    assert not report.suite_blocked
    assert fine.diff and "new_call" in fine.diff


def test_partial_out_is_written_after_each_site(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "good.py", "def fine(client):\n    return client.old_call(1)\n")
    _client(root)
    _write(
        root / "checks" / "test_it.py",
        """\
        import good
        from support import Client

        def test_fine():
            assert good.fine(Client()) == 1
        """,
    )
    baseline = record_baseline(PYTEST, root)
    partial = tmp_path / "partial.json"
    client = FakeModelClient(responses=[_edit("fine", "client.new_call(1)"), "why"])
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_a: True,
            baseline=baseline,
            partial_out=partial,
        ),
    )
    assert partial.is_file()
    data = json.loads(partial.read_text(encoding="utf-8"))
    assert data["tokens_used"] == report.tokens_used
    assert data["results"]
    assert report.tokens_used > 0


def test_sigterm_during_apply_writes_partial_and_stops(tmp_path: Path, monkeypatch):
    root = tmp_path / "app"
    _write(root / "a.py", "def one(client):\n    return client.old_call(1)\n")
    _write(root / "b.py", "def two(client):\n    return client.old_call(2)\n")
    _client(root)
    _write(
        root / "checks" / "test_it.py",
        """\
        import a, b
        from support import Client

        def test_one():
            assert a.one(Client()) == 1

        def test_two():
            assert b.two(Client()) == 2
        """,
    )
    baseline = record_baseline(PYTEST, root)
    partial = tmp_path / "partial.json"
    calls = {"n": 0}

    def _edit_then_signal(turns, model=""):
        calls["n"] += 1
        if calls["n"] == 1:
            # After the first propose returns, ask apply to stop (as timeout would).
            os.kill(os.getpid(), signal.SIGTERM)
            return _edit("one", "client.new_call(1)")
        return _edit("two", "client.new_call(2)")

    client = FakeModelClient(responses=[_edit_then_signal, "why", _edit("two", "client.new_call(2)"), "why"])
    report = apply_sites(
        _sites(("a.py", 2, "client.old_call"), ("b.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_a: True,
            baseline=baseline,
            partial_out=partial,
        ),
    )
    assert partial.is_file()
    data = json.loads(partial.read_text(encoding="utf-8"))
    assert data["tokens_used"] >= 0
    assert any(
        item["status"] in {
            SiteStatus.FIXED_TESTS_COULDNT_RUN.value,
            SiteStatus.SKIPPED.value,
            SiteStatus.FIXED_UNVERIFIED.value,
            SiteStatus.FIXED_VERIFIED.value,
            SiteStatus.FIXED_NOT_COVERED.value,
            SiteStatus.NEEDS_HUMAN.value,
        }
        for item in data["results"]
    )
    # Never leave a verified claim when the run was interrupted.
    assert all(item["status"] != SiteStatus.FIXED_VERIFIED.value for item in report.to_dict()["results"])
