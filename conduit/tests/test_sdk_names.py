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
from conduit.apply.names import (
    DOES_NOT_EXIST,
    ground_candidates,
    merge_candidates,
    name_breaks,
    public_surface,
)
from conduit.apply.prompts import propose_messages
from conduit.apply.signatures import _modules
from conduit.apply.report import format_report

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


def test_public_surface_lists_real_modules_classes_methods(tmp_path: Path):
    modules = _modules_v2(tmp_path)
    surface = public_surface(modules, "wirekit")
    assert "wirekit.Client" in surface
    assert "wirekit.client.Client.send" in surface or "wirekit.Client.send" in surface
    assert "wirekit.errors.WireError" in surface or "wirekit.WireError" in surface
    assert "wirekit.chat" not in surface
    assert all("chat" not in name for name in surface)


def test_ground_candidates_ranks_send_near_old_call(tmp_path: Path):
    modules = _modules_v2(tmp_path)
    rows = ground_candidates(
        modules,
        package="wirekit",
        hints=["wirekit.Client.old_call", "client.old_call", "old_call"],
        limit=16,
    )
    assert 1 <= len(rows) <= 16
    symbols = [row["symbol"] for row in rows]
    assert any(name.endswith(".send") or name.endswith(".Client") for name in symbols)
    assert all("chat" not in name and "Completions" not in name for name in symbols)
    assert all("signature" in row for row in rows)


def test_merge_candidates_prefers_existing_then_grounds():
    merged = merge_candidates(
        [{"symbol": "wirekit.Client.send", "signature": "send(self, value)"}],
        [
            {"symbol": "wirekit.Client.send", "signature": "dup"},
            {"symbol": "wirekit.Client.ping", "signature": "ping(self)"},
        ],
        limit=16,
    )
    assert [row["symbol"] for row in merged] == [
        "wirekit.Client.send",
        "wirekit.Client.ping",
    ]


def test_grounding_flows_into_propose_prompt(tmp_path: Path):
    root = _repo(tmp_path)
    fixtures = _fixture(tmp_path)
    good = _edit("def broken(client):\n    return client.send(1)\n")
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
    assert report.results[0].status == SiteStatus.FIXED_VERIFIED, report.results[0].error
    prompt = client.calls[0]["turns"][-1].content
    assert "candidates" in prompt
    assert "Do not invent" in client.calls[0]["turns"][0].content or "do not invent" in client.calls[
        0
    ]["turns"][0].content.lower()
    # Real target names appear in the context pack JSON.
    assert "wirekit.Client" in prompt or "Client.send" in prompt or ".send" in prompt


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


def test_name_breaks_all_imports_flags_stale_unused_package_import(tmp_path: Path):
    """Patched-file mode: every package import is checked, even if unused in span."""
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
    found = name_breaks(
        modules,
        "mod.py",
        source,
        (3, 4),
        "wirekit",
        "2.0",
        original=original,
        all_imports=True,
    )
    assert found
    assert "RemovedError" in found[0]
    assert DOES_NOT_EXIST in found[0]


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
    assert report.invented_names_caught >= 1
    assert report.name_check_retries == 1
    assert "invented_names_caught:" in format_report(report)
    assert "name_check_retries:" in format_report(report)
    data = report.to_dict()
    assert data["summary"]["invented_names_caught"] >= 1
    assert data["summary"]["name_check_retries"] == 1


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
    assert report.invented_names_caught >= 2
    assert report.name_check_retries == 1


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
    assert all(
        DOES_NOT_EXIST not in call["turns"][-1].content
        for call in client.calls
        if call["turns"]
    )
    assert report.invented_names_caught == 0
    assert report.name_check_retries == 0


def test_propose_messages_forbid_invented_names_without_apply(tmp_path: Path):
    """Prompt rules exist even when inspecting a pack offline."""
    from conduit.apply.context import build_context_pack
    from conduit.apply.impact_input import sites_from_payload

    root = _repo(tmp_path)
    site = sites_from_payload(_payload())[0]
    pack = build_context_pack(root, site)
    pack.candidates = [
        {"symbol": "wirekit.Client.send", "signature": "wirekit.Client.send(self, value: int) -> str"}
    ]
    system, user = propose_messages(pack)
    assert "do not invent" in system[1].lower()
    assert "candidates" in user[1]


def test_patched_file_stale_package_import_triggers_name_retry(tmp_path: Path):
    """Apply recheck with all_imports: leftover bad import is caught and retried."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        textwrap.dedent(
            """\
            from wirekit import RemovedError

            def broken(client):
                return client.old_call(1)
            """
        ),
        encoding="utf-8",
    )
    fixtures = _fixture(tmp_path)
    payload = _payload()
    payload["impacts"][0]["sites"][0]["line"] = 4
    # Fixes the call but leaves the stale RemovedError import → name check fails,
    # one retry with the same shape still fails → needs-human, file untouched.
    first = _edit("def broken(client):\n    return client.send(1)\n")
    client = FakeModelClient(responses=[first, first, "should not run"])
    report = apply_sites(
        sites_from_payload(payload),
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
    assert result.status == SiteStatus.NEEDS_HUMAN, (result.status, result.error)
    assert DOES_NOT_EXIST in (result.error or "")
    assert "RemovedError" in (result.error or "")
    assert report.invented_names_caught >= 1
    assert report.name_check_retries == 1
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "RemovedError" in text
    assert "old_call" in text
