"""Rewrites must not invent names missing from the target release.

Uses a made-up library only (wirekit). No real SDK names.
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.names import DOES_NOT_EXIST, name_breaks
from conduit.apply.signatures import _modules

from test_apply import _EXERCISE, _settings


def _wirekit_v2() -> dict[str, str]:
    """Target release surface: Client, WireError. No chat.completions, no RemovedError."""
    return {
        "wirekit/__init__.py": "from .client import Client\nfrom .errors import WireError\n",
        "wirekit/client.py": textwrap.dedent(
            """\
            class Client:
                def send(self, value: int) -> str:
                    return ""

                def ping(self) -> str:
                    return ""
            """
        ),
        "wirekit/errors.py": textwrap.dedent(
            """\
            class WireError(Exception):
                pass
            """
        ),
    }


def _fixture(tmp_path: Path) -> Path:
    fixtures = tmp_path / "fixtures"
    root = fixtures / "new"
    for rel, text in _wirekit_v2().items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"wirekit": {"wheels": {"2.0": "new"}}}}),
        encoding="utf-8",
    )
    return fixtures


def _modules_v2(tmp_path: Path) -> dict[str, ast.Module]:
    return _modules(str(_fixture(tmp_path) / "new"))


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        textwrap.dedent(
            """\
            def broken(client):
                return client.old_call(1)
            """
        ),
        encoding="utf-8",
    )
    return root


def _payload() -> dict:
    return {
        "dependency": "wirekit",
        "impacts": [
            {
                "dependency": "wirekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "parameter old_call renamed to send on Client",
                "confidence": "high",
                "source": "wheel",
                "kind": "param_renamed",
                "symbol": "wirekit.Client.send",
                "old": "old_call",
                "new": "send",
                "sites": [
                    {
                        "path": "mod.py",
                        "line": 2,
                        "expr": "client.old_call",
                        "confidence": "high",
                        "via_wrapper": False,
                    }
                ],
            }
        ],
        "deprecations": [],
        "diffed": ["wirekit"],
        "notes": [],
    }


def _edit(source: str, imports: list[str] | None = None) -> str:
    return json.dumps({"function_source": source, "new_imports": imports or []})


def test_name_breaks_rejects_invented_nested_import(tmp_path: Path):
    modules = _modules_v2(tmp_path)
    source = textwrap.dedent(
        """\
        from wirekit.chat.completions import Completions

        def broken(client):
            return Completions()
        """
    )
    found = name_breaks(modules, "mod.py", source, (3, 4), "wirekit", "2.0", original="")
    assert found, found
    assert DOES_NOT_EXIST in found[0]
    assert "wirekit.chat" in found[0]
    assert "names in wirekit:" in found[0]
    assert "Client" in found[0] or "errors" in found[0]


def test_name_breaks_rejects_removed_exception_import(tmp_path: Path):
    modules = _modules_v2(tmp_path)
    original = textwrap.dedent(
        """\
        def broken(client):
            return client.old_call(1)
        """
    )
    source = textwrap.dedent(
        """\
        from wirekit import RemovedError

        def broken(client):
            raise RemovedError()
        """
    )
    found = name_breaks(modules, "mod.py", source, (3, 4), "wirekit", "2.0", original=original)
    assert len(found) == 1
    assert "RemovedError" in found[0]
    assert DOES_NOT_EXIST in found[0]
    assert "WireError" in found[0] or "Client" in found[0]


def test_name_breaks_rejects_wrong_module_attribute_chain(tmp_path: Path):
    modules = _modules_v2(tmp_path)
    source = textwrap.dedent(
        """\
        import wirekit

        def broken(client):
            return wirekit.nested.Client()
        """
    )
    found = name_breaks(modules, "mod.py", source, (3, 4), "wirekit", "2.0", original="")
    assert found
    assert "wirekit.nested" in found[0]
    assert DOES_NOT_EXIST in found[0]


def test_name_breaks_accepts_real_names(tmp_path: Path):
    modules = _modules_v2(tmp_path)
    source = textwrap.dedent(
        """\
        from wirekit import Client
        from wirekit.errors import WireError

        def broken(client):
            try:
                return client.send(1)
            except WireError:
                return Client().ping()
        """
    )
    assert name_breaks(modules, "mod.py", source, (4, 8), "wirekit", "2.0", original="") == []


def test_name_breaks_ignores_preexisting_unused_bad_import(tmp_path: Path):
    """An unused bad import the rewrite did not touch is not this site's finding."""
    modules = _modules_v2(tmp_path)
    original = textwrap.dedent(
        """\
        from wirekit import RemovedError

        def broken(client):
            return client.old_call(1)
        """
    )
    source = textwrap.dedent(
        """\
        from wirekit import RemovedError

        def broken(client):
            return client.send(1)
        """
    )
    assert name_breaks(modules, "mod.py", source, (3, 4), "wirekit", "2.0", original=original) == []


def test_invented_import_rejected_then_fixed_on_retry(tmp_path: Path):
    root = _repo(tmp_path)
    fixtures = _fixture(tmp_path)
    bad = _edit(
        "def broken(client):\n    return Completions().run(client)\n",
        ["from wirekit.chat.completions import Completions"],
    )
    good = _edit("def broken(client):\n    return client.send(1)\n")
    client = FakeModelClient(responses=[bad, good, "renamed old_call to send"])
    report = apply_sites(
        sites_from_payload(_payload()),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            write=True,
            test_cmd=_EXERCISE,
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    result = report.results[0]
    assert result.status == SiteStatus.FIXED_VERIFIED, result.error
    assert result.attempts == 2
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "client.send(1)" in text
    assert "Completions" not in text
    retry = client.calls[1]["turns"][-1].content
    assert DOES_NOT_EXIST in retry
    assert "wirekit.chat" in retry or "Completions" in retry


def test_invented_import_still_invalid_after_retry_is_needs_human(tmp_path: Path):
    root = _repo(tmp_path)
    fixtures = _fixture(tmp_path)
    original = (root / "mod.py").read_text(encoding="utf-8")
    bad = _edit(
        "def broken(client):\n    raise RemovedError()\n",
        ["from wirekit import RemovedError"],
    )
    also_bad = _edit(
        "def broken(client):\n    return Completions()\n",
        ["from wirekit.chat.completions import Completions"],
    )
    client = FakeModelClient(responses=[bad, also_bad, "should not be used"])
    report = apply_sites(
        sites_from_payload(_payload()),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            write=True,
            test_cmd=_EXERCISE,
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    result = report.results[0]
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert result.status != SiteStatus.FIXED_VERIFIED
    assert "SDK name still missing after one retry" in result.error
    assert DOES_NOT_EXIST in result.error
    assert result.attempts == 2
    assert (root / "mod.py").read_text(encoding="utf-8") == original
    assert len(client.calls) == 2


def test_valid_sdk_names_pass_untouched(tmp_path: Path):
    root = _repo(tmp_path)
    fixtures = _fixture(tmp_path)
    good = _edit(
        "def broken(client):\n"
        "    _exc = WireError\n"
        "    return client.send(1)\n",
        ["from wirekit.errors import WireError"],
    )
    client = FakeModelClient(responses=[good, "switched to send"])
    report = apply_sites(
        sites_from_payload(_payload()),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            write=True,
            test_cmd=_EXERCISE,
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    result = report.results[0]
    assert result.status == SiteStatus.FIXED_VERIFIED, result.error
    assert result.attempts == 1
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "client.send(1)" in text
    assert "WireError" in text
    assert DOES_NOT_EXIST not in (result.error or "")
    assert all(DOES_NOT_EXIST not in call["turns"][-1].content for call in client.calls if call["turns"])
