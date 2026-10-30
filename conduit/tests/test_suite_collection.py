"""Apply when the SDK bump breaks test collection (issue #141).

Made-up ``widgetkit`` only (fixtures/removed-api-sdk). v2 removes
``widgetkit.error`` and ``Widget.create``; tests that import or patch those
symbols fail at collection. Source fixes must not be marked "not covered" or
"verified" when the suite could not run, and call sites in one file move
together so a file is never left half-migrated.
"""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import removed_from_payload, sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.report import format_report
from conduit.apply.testrefs import find_test_refs, is_test_path
from conduit.apply.testrun import record_baseline
from test_apply import _settings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "removed-api-sdk"
PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _install_sdk(root: Path, version: str) -> None:
    dest = root / "widgetkit"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURES / version / "widgetkit", dest)


def _edit_make() -> str:
    return json.dumps(
        {
            "function_source": "def make(name):\n    return Widget.build(name)\n",
            "new_imports": [],
        }
    )


def _edit_again() -> str:
    return json.dumps(
        {
            "function_source": "def again(name):\n    return Widget.build(name)\n",
            "new_imports": [],
        }
    )


def _edit_fine() -> str:
    return json.dumps(
        {
            "function_source": "def fine(name):\n    return Widget.build(name)\n",
            "new_imports": [],
        }
    )


def _payload(*specs: tuple[str, int, str]) -> dict:
    return {
        "dependency": "widgetkit",
        "impacts": [
            {
                "dependency": "widgetkit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "Widget.create was removed; use Widget.build",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": "widgetkit.Widget.create",
                "old": "create",
                "new": "build",
                "candidates": [{"symbol": "widgetkit.Widget.build", "score": 1.0}],
                "sites": [
                    {
                        "path": path,
                        "line": line,
                        "expr": expr,
                        "confidence": "high",
                        "via_wrapper": False,
                    }
                    for path, line, expr in specs
                ],
            },
            {
                "dependency": "widgetkit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "widgetkit.error.GoneError was removed",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": "widgetkit.error.GoneError",
                "sites": [],
            },
        ],
        "deprecations": [],
        "diffed": ["widgetkit"],
        "notes": [],
    }


def _sites(*specs: tuple[str, int, str]):
    payload = _payload(*specs)
    return sites_from_payload(payload), removed_from_payload(payload)


def _config(root: Path, client, baseline, removed) -> ApplyConfig:
    return ApplyConfig(
        root=root,
        settings=_settings(max_attempts=1),
        write=True,
        test_cmd=PYTEST,
        client=client,
        site_cleared=lambda *_a: True,
        baseline=baseline,
        removed_symbols=removed,
    )


def test_test_path_helper_recognizes_common_layouts():
    assert is_test_path("checks/test_it.py")
    assert is_test_path("tests/unit/test_x.py")
    assert is_test_path("pkg/conftest.py")
    assert is_test_path("pkg/foo_test.py")
    assert not is_test_path("pkg/client.py")


def test_find_test_refs_reports_import_and_string_patch(tmp_path: Path):
    root = tmp_path / "app"
    _write(
        root / "checks" / "test_widgets.py",
        """\
        from unittest.mock import patch
        from widgetkit.error import GoneError
        import widgetkit

        @patch("widgetkit.Widget.create")
        def test_create(mock_create):
            mock_create.return_value = None
            assert GoneError is not None
        """,
    )
    _write(root / "app.py", "from widgetkit import Widget\n\ndef make():\n    return Widget.create('a')\n")
    sites, removed = _sites(("app.py", 4, "Widget.create"))
    refs = find_test_refs(root, sites, extras=removed)
    by_kind = {(item.kind, item.symbol) for item in refs}
    assert ("import", "widgetkit.error.GoneError") in by_kind
    assert ("patch", "widgetkit.Widget.create") in by_kind
    for item in refs:
        assert item.path == "checks/test_widgets.py"
        assert item.line > 0
        assert item.code


def test_collection_failure_is_not_not_covered(tmp_path: Path):
    """gpt-review case: suite fails at collection; status must not say not covered."""
    root = tmp_path / "app"
    _install_sdk(root, "v1")
    _write(
        root / "app.py",
        """\
        from widgetkit import Widget

        def make(name):
            return Widget.create(name)
        """,
    )
    _write(
        root / "checks" / "test_app.py",
        """\
        from widgetkit.error import GoneError
        import app

        def test_make():
            assert app.make("x").name == "x"
            assert GoneError is not None
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    _install_sdk(root, "v2")
    sites, removed = _sites(("app.py", 4, "Widget.create"))
    client = FakeModelClient(
        responses=[
            _edit_make(),
            "Uses build.",
            # No test-file rewrite response: refs stay reported.
        ]
    )
    report = apply_sites(sites, _config(root, client, baseline, removed))
    (result,) = report.results
    assert "build(name)" in (root / "app.py").read_text(encoding="utf-8")
    assert result.status == SiteStatus.FIXED_TESTS_COULDNT_RUN, (result.status, result.error, result.why)
    assert result.status != SiteStatus.FIXED_VERIFIED
    assert result.status != SiteStatus.FIXED_NOT_COVERED
    assert "couldn't run" in result.status.value
    assert report.test_refs
    assert any(item["symbol"] == "widgetkit.error.GoneError" for item in report.test_refs)
    assert "tests reference removed API" in format_report(report)
    assert report.to_dict()["summary"]["fixed, tests couldn't run"] == 1
    assert report.to_dict()["summary"]["fixed, not covered by tests"] == 0


def test_file_sites_move_together_on_collection_failure(tmp_path: Path):
    """Two call sites in one file are kept or reverted together on collection errors."""
    root = tmp_path / "app"
    _install_sdk(root, "v1")
    _write(
        root / "app.py",
        """\
        from widgetkit import Widget

        def make(name):
            return Widget.create(name)

        def again(name):
            return Widget.create(name)
        """,
    )
    _write(
        root / "other.py",
        """\
        from widgetkit import Widget

        def fine(name):
            return Widget.create(name)
        """,
    )
    _write(
        root / "checks" / "test_ok.py",
        """\
        import other

        def test_fine():
            assert other.fine("y").name == "y"
        """,
    )
    _write(
        root / "checks" / "test_broken.py",
        """\
        from widgetkit.error import GoneError
        import app

        def test_make():
            assert app.make("x").name == "x"
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable
    _install_sdk(root, "v2")
    sites, removed = _sites(
        ("app.py", 4, "Widget.create"),
        ("app.py", 7, "Widget.create"),
        ("other.py", 4, "Widget.create"),
    )
    client = FakeModelClient(
        responses=[
            _edit_make(),
            "why",
            _edit_again(),
            "why",
            _edit_fine(),
            "why",
        ]
    )
    report = apply_sites(sites, _config(root, client, baseline, removed))
    by_key = {(item.site.path, item.site.line): item for item in report.results}
    app_a = by_key[("app.py", 4)]
    app_b = by_key[("app.py", 7)]
    # Both app.py sites share one fate: not a half-migrated file with one
    # verified and one reverted from the same collection error.
    assert app_a.status == app_b.status, (app_a.status, app_b.status, app_a.why, app_b.why)
    assert app_a.status != SiteStatus.FIXED_VERIFIED
    assert app_a.status != SiteStatus.FIXED_NOT_COVERED
    text = (root / "app.py").read_text(encoding="utf-8")
    creates = text.count("Widget.create")
    builds = text.count("Widget.build")
    assert creates == 0 or builds == 0
    assert creates + builds == 2


def test_test_refs_rewritten_alongside_source_fix(tmp_path: Path):
    """When the model updates the test file, collection can succeed and verify."""
    root = tmp_path / "app"
    _install_sdk(root, "v1")
    _write(
        root / "app.py",
        """\
        from widgetkit import Widget

        def make(name):
            return Widget.create(name)
        """,
    )
    _write(
        root / "checks" / "test_app.py",
        """\
        from unittest.mock import patch
        from widgetkit.error import GoneError
        import app

        @patch("widgetkit.Widget.create")
        def test_make(mock_create):
            mock_create.return_value = type("W", (), {"name": "x"})()
            assert app.make("x").name == "x"
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing
    _install_sdk(root, "v2")
    fixed_test = textwrap.dedent(
        """\
        from unittest.mock import patch
        import app

        @patch("widgetkit.Widget.build")
        def test_make(mock_build):
            mock_build.return_value = type("W", (), {"name": "x"})()
            assert app.make("x").name == "x"
        """
    )
    sites, removed = _sites(("app.py", 4, "Widget.create"))
    client = FakeModelClient(
        responses=[
            _edit_make(),
            "Uses build.",
            json.dumps({"file_source": fixed_test}),
        ]
    )
    report = apply_sites(sites, _config(root, client, baseline, removed))
    (result,) = report.results
    assert "Widget.build" in (root / "app.py").read_text(encoding="utf-8")
    test_text = (root / "checks" / "test_app.py").read_text(encoding="utf-8")
    assert "GoneError" not in test_text
    assert 'patch("widgetkit.Widget.build")' in test_text
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error, result.why)
    assert report.test_refs == []
