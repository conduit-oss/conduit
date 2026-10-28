"""Pass/fail ignores the line recorder. Bisection matches test ids. Flakes are rerun.

Made-up library only. A timing check fails when a tracer is installed and passes
without one. Two module-level rewrites import each other, so a subset fails to
import instead of failing the original test. A third rewrite is independent.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.dropped import content_dropped
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.report import format_report
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.testrun import isolate_pytest, record_baseline
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


def test_a_timing_check_that_fails_only_under_a_tracer_is_not_blamed(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "work.py", "def fine(client):\n    return client.old_call(1)\n")
    _client(root)
    _write(
        root / "checks" / "test_budget.py",
        """\
        import sys
        import time

        import work
        from support import Client

        def test_fine():
            assert work.fine(Client()) == 1

        def test_budget():
            start = time.perf_counter()
            time.sleep(0.01)
            if sys.gettrace() is not None:
                time.sleep(0.6)
            assert time.perf_counter() - start < 0.4
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    client = FakeModelClient(responses=[_edit("fine", "client.new_call(1)"), "why"])
    report = apply_sites(
        _sites(("work.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_args: True,
            baseline=baseline,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error, result.why)
    assert "reverted" not in (result.error or "")
    assert "new_call(1)" in (root / "work.py").read_text(encoding="utf-8")
    assert report.new_failures == []
    assert "line recorder" in (result.why or "") or "line recorder" in report.trace_note


def test_codependent_module_rewrites_stay_one_group(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "fine.py", "def fine(client):\n    return client.old_call(1)\n")
    _write(root / "left.py", "MARK = 0\n")
    _write(root / "right.py", "MARK = 0\n")
    _client(root)
    _write(
        root / "checks" / "test_fine.py",
        """\
        import fine
        from support import Client

        def test_fine():
            assert fine.fine(Client()) == 1
        """,
    )
    _write(
        root / "checks" / "test_pair.py",
        """\
        import left
        import right

        def test_pair():
            assert left.MARK == 1 and right.MARK == 1

        def test_clock():
            if left.MARK == 1 and right.MARK == 1:
                assert False, "clock"
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert "test_clock" not in "".join(baseline.failing)
    assert any("test_pair" in item for item in baseline.failing)
    left = '{"function_source": "from right import PIECE\\nMARK = PIECE\\n", "new_imports": []}'
    right = '{"function_source": "PIECE = 1\\nMARK = 1\\n", "new_imports": []}'
    client = FakeModelClient(
        responses=[_edit("fine", "client.new_call(1)"), "why", left, "why", right, "why"]
    )
    report = apply_sites(
        _sites(("fine.py", 2, "client.old_call"), ("left.py", 1, "MARK"), ("right.py", 1, "MARK")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_args: True,
            baseline=baseline,
        ),
    )
    by_path = {item.site.path: item for item in report.results}
    assert by_path["fine.py"].status == SiteStatus.FIXED_VERIFIED, (by_path["fine.py"].status, by_path["fine.py"].error)
    assert "new_call(1)" in (root / "fine.py").read_text(encoding="utf-8")
    assert by_path["left.py"].status == SiteStatus.NEEDS_HUMAN, (by_path["left.py"].error, by_path["left.py"].why)
    assert by_path["right.py"].status == SiteStatus.NEEDS_HUMAN, (by_path["right.py"].error, by_path["right.py"].why)
    assert "reverted" in (by_path["left.py"].error or "")
    assert "reverted" in (by_path["right.py"].error or "")


def test_a_failure_that_passes_alone_is_flaky_not_ours(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "work.py", "def fine(client):\n    return client.old_call(1)\n")
    _client(root)
    _write(
        root / "checks" / "test_blip.py",
        """\
        from pathlib import Path

        def test_blip():
            text = Path("work.py").read_text(encoding="utf-8")
            mark = Path("blip.mark")
            if "new_call" in text and not mark.exists():
                mark.write_text("1", encoding="utf-8")
                assert False, "blip"
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    client = FakeModelClient(responses=[_edit("fine", "client.new_call(1)"), "why"])
    report = apply_sites(
        _sites(("work.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_args: True,
            baseline=baseline,
        ),
    )
    (result,) = report.results
    assert result.status != SiteStatus.NEEDS_HUMAN, (result.status, result.error, result.why)
    assert "reverted" not in (result.error or "")
    assert "new_call(1)" in (root / "work.py").read_text(encoding="utf-8")
    assert report.flaky and "test_blip" in report.flaky[0]
    assert "flaky, not ours" in format_report(report)


def test_a_filtered_value_is_not_verified(tmp_path: Path):
    root = tmp_path / "app"
    _write(
        root / "work.py",
        """\
        def keep_loud(items):
            return [item for item in items if item["tag"] != "quiet"]

        def send(client):
            return client.old_call([
                {"tag": "quiet", "body": "preface"},
                {"tag": "loud", "body": "hello"},
            ])
        """,
    )
    _client(root)
    _write(
        root / "checks" / "test_send.py",
        """\
        import work
        from support import Client

        def test_send():
            assert "hello" in str(work.send(Client()))
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    edited = "\n".join(
        [
            "def send(client):",
            "    return client.new_call(keep_loud([",
            '        {"tag": "quiet", "body": "preface"},',
            '        {"tag": "loud", "body": "hello"},',
            "    ]))",
            "",
        ]
    )
    client = FakeModelClient(responses=[json.dumps({"function_source": edited, "new_imports": []}), "why"])
    line = next(i for i, text in enumerate((root / "work.py").read_text().splitlines(), 1) if "old_call" in text)
    report = apply_sites(
        _sites(("work.py", line, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_args: True,
            baseline=baseline,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_NEEDS_REVIEW, (result.status, result.error, result.why)
    assert result.status != SiteStatus.FIXED_VERIFIED
    assert "preface" in (result.why or "")
    assert result.why.startswith("drops ")


def test_content_dropped_follows_a_helper_and_ignores_an_unchanged_call():
    original = "\n".join(
        [
            "def send(client):",
            "    return client.old_call([",
            '        {"tag": "quiet", "body": "preface"},',
            '        {"tag": "loud", "body": "hello"},',
            "    ])",
            "",
        ]
    )
    patched = "\n".join(
        [
            "def keep_loud(items):",
            '    return [item for item in items if item["tag"] != "quiet"]',
            "",
            "def send(client):",
            "    return client.new_call(keep_loud([",
            '        {"tag": "quiet", "body": "preface"},',
            '        {"tag": "loud", "body": "hello"},',
            "    ]))",
            "",
        ]
    )
    assert "preface" in content_dropped(original, patched, 2)
    assert "hello" not in content_dropped(original, patched, 2)
    assert content_dropped(original, original, 2) == []


def test_isolate_pytest_keeps_flags_and_rejects_other_runners():
    command = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"
    assert isolate_pytest(command, "checks/test_budget.py::test_budget") == (
        f"{sys.executable} -m pytest -q -p no:cacheprovider checks/test_budget.py::test_budget"
    )
    assert isolate_pytest("make check", "checks/test_budget.py::test_budget") is None
