"""A fix counts as verified only when a test runs the lines it changed.

The made-up ``orchard`` repo below has one function its tests call and one they
never reach, a module constant read by each, an unread constant and an import.
Lines are recorded while the after-fix tests run; a rewrite whose changed
statements did not all run is "fixed, not covered by tests".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.covered import block_reach, changed_lines, describe, recording
from conduit.apply.covered import tests_ran as _ran
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus

from test_apply import _impact_payload, _settings

_ORCHARD = textwrap.dedent(
    """\
    import json

    BASKET = 3
    CRATE = 12
    LOOSE = 9


    def pick(n):
        if n > 100:
            return "too many"
        return json.dumps({"n": n, "basket": BASKET})


    def pack(n):
        return n * CRATE
    """
)
_TESTS = "from orchard import pick\n\n\ndef test_pick():\n    assert pick(1)\n"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "orchard.py").write_text(_ORCHARD, encoding="utf-8")
    (root / "tests" / "test_orchard.py").write_text(_TESTS, encoding="utf-8")
    return root


def _record(root: Path, command: str | None = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    with recording(root, ["orchard.py"], env) as record:
        proc = subprocess.run(
            command or f"{sys.executable} -m pytest -q -p no:cacheprovider tests",
            shell=True, cwd=root, env=env, capture_output=True, text=True, check=False,
        )
    record.tests_ran = _ran({}, proc.stdout + proc.stderr)
    return record, proc


def _reach(record, block: str, original: str = ""):
    return block_reach(record, "orchard.py", original or _ORCHARD.replace(block, "# was here\n"), block)


def test_lines_a_test_runs_are_covered_and_others_are_not(tmp_path: Path):
    record, proc = _record(_repo(tmp_path))
    assert proc.returncode == 0 and record.tests_ran and not record.replaced
    assert _reach(record, '    return json.dumps({"n": n, "basket": BASKET})\n').covered
    missed = _reach(record, '        return "too many"\n')
    assert not missed.covered and missed.missed == [(10, 10)]
    assert describe("orchard.py", missed.missed) == "orchard.py:10"
    assert not _reach(record, "    return n * CRATE\n").covered


def test_module_level_lines_count_honestly(tmp_path: Path):
    record, _ = _record(_repo(tmp_path))
    # Read by a function a test ran: covered.
    assert _reach(record, "BASKET = 3\n").covered
    # Ran on import, but only a function no test reaches reads it: not proven.
    assert not _reach(record, "CRATE = 12\n").covered
    # Nothing reads it: running on import is all there is to prove.
    assert _reach(record, "LOOSE = 9\n").covered
    # An import is proven by running.
    assert _reach(record, "import json\n").covered


def test_a_multi_line_rewrite_needs_every_changed_statement_to_run(tmp_path: Path):
    record, _ = _record(_repo(tmp_path))
    block = '    if n > 100:\n        return "too many"\n    return json.dumps({"n": n, "basket": BASKET})\n'
    original = _ORCHARD.replace('return "too many"', 'return "over"').replace('"basket": BASKET', '"b": BASKET')
    found = _reach(record, block, original)
    assert not found.covered and found.missed == [(10, 10)]
    # Only the unchanged ``if`` line and a line that ran differ: covered.
    original = _ORCHARD.replace('"basket": BASKET', '"b": BASKET')
    assert _reach(record, block, original).covered


def test_no_test_run_proves_nothing(tmp_path: Path):
    record, _ = _record(_repo(tmp_path), f"{sys.executable} -c \"import orchard; orchard.pick(1)\"")
    assert not record.tests_ran
    assert not _reach(record, '    return json.dumps({"n": n, "basket": BASKET})\n').covered
    assert not block_reach(None, "orchard.py", _ORCHARD, "BASKET = 3\n").covered


def test_a_repo_sitecustomize_still_runs(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "sitecustomize.py").write_text(
        "import pathlib\npathlib.Path('site-ran').write_text('yes')\n", encoding="utf-8"
    )
    record, proc = _record(root)
    assert proc.returncode == 0 and (root / "site-ran").read_text() == "yes"
    assert _reach(record, '    return json.dumps({"n": n, "basket": BASKET})\n').covered


def test_changed_lines_counts_a_deletion_where_it_was():
    before = "a\nb\nc\nd\n"
    assert changed_lines(before, "a\nB\nc\nd\n", 1, 4) == [2]
    assert changed_lines(before, "a\nc\nd\n", 1, 3) == [2]


# --- through apply ---------------------------------------------------------------------

_MOD = textwrap.dedent(
    """\
    def reached(client):
        return 1


    def broken(client):
        return client.old_call(1)
    """
)


def _apply(tmp_path: Path, test_cmd: str):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(_MOD, encoding="utf-8")
    edit = json.dumps({"function_source": "def broken(client):\n    return client.new_call(1)\n", "new_imports": []})
    client = FakeModelClient(responses=[edit, "Uses new_call."])
    report = apply_sites(
        sites_from_payload(_impact_payload(line=6)),
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd=test_cmd, client=client,
                    site_cleared=lambda *_a: True),
    )
    return report, report.results[0]


def test_a_fix_no_test_reaches_is_not_verified(tmp_path: Path):
    command = f"{sys.executable} -c \"import mod; mod.reached(None); print('1 passed')\""
    report, result = _apply(tmp_path, command)
    assert result.status == SiteStatus.FIXED_NOT_COVERED
    assert result.status.value == "fixed, not covered by tests"
    assert result.why.startswith("No test runs the changed lines (mod.py:6), so this fix is unproven.")
    assert "Uses new_call." in result.why
    assert "new_call" in (tmp_path / "app" / "mod.py").read_text()  # still written
    assert report.to_dict()["summary"]["fixed, not covered by tests"] == 1
    assert report.to_dict()["summary"]["fixed (verified)"] == 0


def test_a_fix_a_test_runs_is_verified(tmp_path: Path):
    command = f"{sys.executable} -c \"import mod, types; mod.broken(types.SimpleNamespace(new_call=abs)); print('1 passed')\""
    _report, result = _apply(tmp_path, command)
    assert result.status == SiteStatus.FIXED_VERIFIED, result.why


def test_a_command_that_runs_no_test_verifies_nothing(tmp_path: Path):
    command = f"{sys.executable} -c \"import mod, types; mod.broken(types.SimpleNamespace(new_call=abs))\""
    _report, result = _apply(tmp_path, command)
    assert result.status == SiteStatus.FIXED_NOT_COVERED
    assert result.why.startswith("No test ran")
