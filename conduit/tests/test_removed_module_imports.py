"""Imports from a module the target release removed are relocated from its surface.

Made-up packages only (acmekit). No real SDK, product, or migration-table names.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.names import RemovedModuleImports, relocate_removed_imports
from conduit.apply.signatures import _modules
from conduit.apply.testrun import record_baseline

from test_apply import _settings

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider checks"


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def _wheel(tmp_path: Path) -> tuple[Path, dict]:
    """Target acmekit: legacy module gone, Kept re-exported, Nested kept, Lost absent."""
    root = tmp_path / "fixtures" / "new"
    _write(
        root / "acmekit" / "__init__.py",
        """\
        from .nested import Nested
        from .kept import Kept
        """,
    )
    _write(
        root / "acmekit" / "kept.py",
        """\
        class Kept:
            pass
        """,
    )
    _write(
        root / "acmekit" / "nested.py",
        """\
        class Nested:
            pass
        """,
    )
    _write(
        root / "acmekit" / "client.py",
        """\
        class Client:
            def ping(self) -> str:
                return "ok"
        """,
    )
    fixtures = tmp_path / "fixtures"
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"acmekit": {"wheels": {"2.0": "new"}}}}),
        encoding="utf-8",
    )
    modules = _modules(str(root))
    return fixtures, modules


def _maps(modules: dict) -> list[RemovedModuleImports]:
    return [
        RemovedModuleImports(
            package="acmekit",
            version="2.0",
            modules=modules,
            wheel=None,
        )
    ]


def test_relocates_every_file_the_same_way_and_leaves_a_live_module(tmp_path: Path):
    _fixtures, modules = _wheel(tmp_path)
    maps = _maps(modules)
    source = (
        "from acmekit.legacy import Kept, Nested, Lost\n"
        "from acmekit.client import Client\n"
        "\n"
        "def use():\n"
        "    return Kept, Nested, Lost, Client\n"
    )
    other = "from acmekit.legacy import Kept, Lost\n\nVALUE = Kept\n"
    first, hits = relocate_removed_imports(source, "a.py", maps)
    second, hits_b = relocate_removed_imports(other, "b.py", maps)
    assert "acmekit.legacy" not in first
    assert "acmekit.legacy" not in second
    assert "from acmekit.client import Client" in first
    assert "from acmekit import Kept, Nested" in first or (
        "from acmekit import Kept" in first and "from acmekit.nested import Nested" in first
    )
    assert "from acmekit import Kept" in second
    assert hits[0].missing == ("Lost",)
    assert hits_b[0].missing == ("Lost",)
    assert hits[0].moved[0][0] == "Kept"
    assert hits_b[0].moved[0][1] == hits[0].moved[0][1]
    # A second pass is a no-op: the removed module is already gone.
    again, again_hits = relocate_removed_imports(first, "a.py", maps)
    assert again == first
    assert again_hits == []


def test_leaves_the_import_when_no_name_has_a_home(tmp_path: Path):
    _fixtures, modules = _wheel(tmp_path)
    source = "from acmekit.legacy import Lost\n\nraise Lost\n"
    updated, hits = relocate_removed_imports(source, "only.py", _maps(modules))
    assert updated == source
    assert hits[0].replacement == ""
    assert hits[0].missing == ("Lost",)
    assert hits[0].moved == ()


def test_runtime_getattr_finds_a_lazy_reexport(tmp_path: Path):
    root = tmp_path / "wheel"
    _write(
        root / "acmekit" / "__init__.py",
        """\
        def __getattr__(name):
            if name == "Kept":
                class Kept:
                    pass
                return Kept
            raise AttributeError(name)
        """,
    )
    modules = _modules(str(root))
    surface_map = RemovedModuleImports(
        package="acmekit",
        version="2.0",
        modules=modules,
        wheel=root,
    )
    source = "from acmekit.legacy import Kept\n"
    updated, hits = relocate_removed_imports(source, "use.py", [surface_map])
    assert hits[0].missing == ()
    assert "from acmekit import Kept" in updated
    assert "acmekit.legacy" not in updated


def _import_payload(*specs: tuple[str, int]) -> dict:
    return {
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "acmekit.legacy removed",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": "acmekit.legacy.Kept",
                "sites": [
                    {
                        "path": path,
                        "line": line,
                        "expr": "import Kept",
                        "confidence": "high",
                        "via_wrapper": False,
                    }
                    for path, line in specs
                ],
            }
        ],
        "deprecations": [],
        "diffed": ["acmekit"],
        "notes": [],
    }


def test_apply_rewrites_every_file_in_one_pass_without_the_model(tmp_path: Path):
    fixtures, _modules_unused = _wheel(tmp_path)
    root = tmp_path / "app"
    _write(root / "a.py", "from acmekit.legacy import Kept, Lost\n\nA = Kept\nERR = Lost\n")
    _write(root / "pkg" / "b.py", "from acmekit.legacy import Kept, Lost\n\nB = Kept\nERR = Lost\n")
    _write(root / "checks" / "test_skip.py", "from acmekit.legacy import Kept\n")
    client = FakeModelClient(responses=[])
    report = apply_sites(
        sites_from_payload(_import_payload(("a.py", 1), ("pkg/b.py", 1))),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=1),
            write=True,
            client=client,
            fixtures=fixtures,
        ),
    )
    assert client.calls == []
    for rel in ("a.py", "pkg/b.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "acmekit.legacy" not in text
        assert "from acmekit import Kept" in text
        assert "Lost" in text  # still used; no invented replacement import
    # Test modules are not edited.
    assert "acmekit.legacy" in (root / "checks" / "test_skip.py").read_text(encoding="utf-8")
    statuses = {item.site.path: item.status for item in report.results}
    assert statuses["a.py"] == SiteStatus.NEEDS_HUMAN
    assert statuses["pkg/b.py"] == SiteStatus.NEEDS_HUMAN
    assert "no equivalent" in report.results[0].why.lower()


def test_collection_failure_caused_by_the_edit_is_not_fixed(tmp_path: Path):
    """An edit that makes collection fail is retried once, then needs-human."""
    root = tmp_path / "app"
    _write(
        root / "app.py",
        """\
        def fine(client):
            return client.old_call(1)
        """,
    )
    _write(
        root / "checks" / "test_app.py",
        """\
        import app

        class _Client:
            def old_call(self, n):
                return n

        def test_fine():
            assert app.fine(_Client()) == 1
        """,
    )
    baseline = record_baseline(PYTEST, root)
    assert baseline.usable and not baseline.failing

    def reply(turns, model=""):
        system = turns[0].content if turns else ""
        if "one short sentence" in system:
            return "Added an import."
        return json.dumps(
            {
                "function_source": "def fine(client):\n    return client.old_call(1)\n",
                "new_imports": ["from acmekit.gone import Missing"],
            }
        )

    client = FakeModelClient(responses=[reply] * 8)
    report = apply_sites(
        sites_from_payload(
            {
                "dependency": "acmekit",
                "impacts": [
                    {
                        "dependency": "acmekit",
                        "from_version": "1.0",
                        "to_version": "2.0",
                        "change": "old_call removed",
                        "confidence": "high",
                        "source": "wheel",
                        "kind": "removed",
                        "symbol": "acmekit.Client.old_call",
                        "sites": [
                            {
                                "path": "app.py",
                                "line": 2,
                                "expr": "client.old_call",
                                "confidence": "high",
                                "via_wrapper": False,
                            }
                        ],
                    }
                ],
                "deprecations": [],
                "diffed": ["acmekit"],
                "notes": [],
            }
        ),
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
    text = (root / "app.py").read_text(encoding="utf-8")
    assert "acmekit.gone" not in text
    (result,) = report.results
    assert result.status == SiteStatus.NEEDS_HUMAN, (result.status, result.error, result.why)
    assert result.status != SiteStatus.FIXED_VERIFIED
    assert result.status != SiteStatus.FIXED_UNVERIFIED
    assert result.status != SiteStatus.FIXED_NOT_COVERED
    assert result.status != SiteStatus.FIXED_NEEDS_REVIEW
    # One corrective retry with the collection error, not a retry storm.
    assert 2 <= len(client.calls) <= 4
    # The retry saw the collection error text.
    retried = any("ModuleNotFoundError" in turn.content or "ImportError" in turn.content
                  for call in client.calls for turn in call["turns"])
    assert retried

