"""One site's failure does not revert the rewrites that still pass.

Made-up crate only. A collection error that also happens with no rewrites on
the bumped tree is not blamed on a fix. A failure one rewrite causes is
reverted on its own.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.testrun import record_baseline
from test_apply import _edit, _impact_payload, _settings

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _sites(*specs: tuple[str, int, str]) -> list:
    payload = _impact_payload(path=specs[0][0], line=specs[0][1])
    payload["impacts"][0]["sites"] = [
        {
            "path": path,
            "line": line,
            "expr": expr,
            "confidence": "high",
            "via_wrapper": False,
        }
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


def test_a_bump_that_already_fails_to_collect_does_not_revert_fixes(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "crate" / "__init__.py", "class Gone:\n    pass\n")
    _write(
        root / "good.py",
        """\
        def fine(client):
            return client.old_call(1)
        """,
    )
    _write(
        root / "bad.py",
        """\
        from crate import Gone

        def stuck(client):
            return client.old_call(1)
        """,
    )
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
    _write(root / "checks" / "test_bad.py", "import bad\n\ndef test_stuck():\n    assert True\n")
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    _write(root / "crate" / "__init__.py", "class Kept:\n    pass\n")
    client = FakeModelClient(
        responses=[_edit("fine", "client.new_call(1)"), "why", "not an edit", "not an edit", "not an edit"]
    )
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call"), ("bad.py", 4, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            test_cmd=PYTEST,
            client=client,
            site_cleared=lambda *_args: True,
            baseline=baseline,
        ),
    )
    fine, stuck = report.results
    assert fine.status == SiteStatus.FIXED_VERIFIED, (fine.status, fine.error, fine.why)
    assert "new_call(1)" in (root / "good.py").read_text(encoding="utf-8")
    assert stuck.status == SiteStatus.NEEDS_HUMAN
    assert "reverted" not in (stuck.error or "")
    assert report.retest_runs >= 1
    assert report.retest_seconds >= 0


def test_one_rewrite_is_reverted_without_sinking_the_other(tmp_path: Path):
    root = tmp_path / "app"
    _write(
        root / "good.py",
        """\
        def fine(client):
            return client.old_call(1)
        """,
    )
    _write(
        root / "bad.py",
        """\
        def stuck(client):
            return client.old_call(1)
        """,
    )
    _client(root)
    # The failure text names neither rewrite: the check prints no path.
    command = (
        f"{sys.executable} -c \"import good, bad; from support import Client; "
        "raise SystemExit(0 if good.fine(Client()) == 1 and bad.stuck(Client()) == 1 else 1)\""
    )
    baseline = record_baseline(command, root)
    client = FakeModelClient(
        responses=[_edit("fine", "client.new_call(1)"), "why", _edit("stuck", "client.new_call(0)"), "why"]
    )
    report = apply_sites(
        _sites(("good.py", 2, "client.old_call"), ("bad.py", 2, "client.old_call")),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            test_cmd=command,
            client=client,
            site_cleared=lambda *_args: True,
            baseline=baseline,
        ),
    )
    fine, stuck = report.results
    # The check is not a test runner, so a kept rewrite is unproven rather than verified.
    assert fine.status == SiteStatus.FIXED_NOT_COVERED, (fine.status, fine.error, fine.why)
    assert stuck.status == SiteStatus.NEEDS_HUMAN and "reverted" in (stuck.error or "")
    assert "new_call(1)" in (root / "good.py").read_text(encoding="utf-8")
    assert "old_call(1)" in (root / "bad.py").read_text(encoding="utf-8")
    assert report.retest_runs >= 1
