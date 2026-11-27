"""Impact accepts unclassified third-party SDKs the repo imports.

Uses the invented ``acmekit`` package under ``tests/fixtures/acmekit`` only.
No external eval repos. The analyzed app is copied into ``tmp_path`` so the
repo scanner does not skip a path that contains a ``tests`` segment.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

from conduit.main import app
from conduit.outside_in.evidence import classify_distribution
from conduit.outside_in.specs import evidence_spec
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[1]
ACMEKIT = REPO / "tests" / "fixtures" / "acmekit"
ACME_REPO = ACMEKIT / "repo"
ACME_FIXTURES = ACMEKIT  # index.json + wheels/


def _materialize_repo(tmp_path: Path) -> Path:
    """Copy the fixture app out of ``tests/`` so sources are scanned."""
    dest = tmp_path / "acmekit-app"
    shutil.copytree(ACME_REPO, dest)
    return dest


def test_acmekit_is_not_evidence_classified(tmp_path: Path):
    """Sanity: no I/O evidence, so the old gate would have refused it."""
    root = _materialize_repo(tmp_path)
    found = classify_distribution("acmekit", "1.0.0", root=root)
    assert found.service is False
    assert evidence_spec("acmekit", "1.0.0", root=root) is None


def test_impact_finds_acmekit_rename_call_sites(tmp_path: Path):
    root = _materialize_repo(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "impact",
            "--path",
            str(root),
            "--dep",
            "acmekit",
            "--fixtures",
            str(ACME_FIXTURES),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["impacts"], payload
    assert any(
        item["dependency"] == "acmekit" and "complete" in item["change"]
        for item in payload["impacts"]
    )
    assert any(
        site["path"] == "app.py" and "complete" in site["expr"]
        for item in payload["impacts"]
        for site in item["sites"]
    )


def test_impact_rejects_stdlib_pytest_and_self_package(tmp_path: Path):
    app_py = tmp_path / "app.py"
    app_py.write_text(
        textwrap.dedent(
            """
            import json
            import pytest
            import localpkg

            def run():
                return json.dumps(localpkg.VALUE)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "localpkg").mkdir()
    (tmp_path / "localpkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "pytest==8.0.0\nlocalpkg==0.1.0\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "localpkg"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    for dep in ("json", "pytest", "localpkg"):
        refused = CliRunner().invoke(
            app,
            ["impact", "--path", str(tmp_path), "--dep", dep, "--fixtures", str(ACME_FIXTURES)],
        )
        assert refused.exit_code == 2, (dep, refused.stdout)
        assert "not an outside service" in refused.stdout, (dep, refused.stdout)
