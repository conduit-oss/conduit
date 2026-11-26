"""Whole-file structural migrations in ``conduit apply`` (acmekit fixtures only)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.config import TokenFactorySettings
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.prompts import propose_whole_file_messages
from conduit.apply.splice import PatchError, parse_whole_file_edit
from conduit.apply.structural import (
    WHOLE_FILE_MAX_LINES,
    qualifies_for_whole_file,
    site_still_present,
)


def _settings(**kwargs) -> TokenFactorySettings:
    return TokenFactorySettings.from_env(api_key="test-key-not-for-logging", **kwargs)


def _acmekit_repo(tmp_path: Path) -> Path:
    """Made-up consumer: module-level acmekit entry points + old error import."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "worker.py").write_text(
        textwrap.dedent(
            """\
            import acmekit
            from acmekit.errors import AcmeError

            def fetch_one(key):
                try:
                    return acmekit.Thing.create(key)
                except AcmeError:
                    return None

            def fetch_two(key):
                try:
                    return acmekit.Thing.create(key, mode="fast")
                except AcmeError:
                    return None

            def ping():
                return acmekit.Thing.create("ping")
            """
        ),
        encoding="utf-8",
    )
    return root


def _acmekit_impacts() -> dict:
    """Three removed module-level sites sharing the acmekit import."""
    cand = [
        {
            "symbol": "acmekit.Client.create",
            "signature": "acmekit.Client.create(self, key, mode=None)",
        }
    ]
    sites = [
        {"path": "worker.py", "line": 6, "expr": "acmekit.Thing.create", "via_wrapper": False},
        {"path": "worker.py", "line": 12, "expr": "acmekit.Thing.create", "via_wrapper": False},
        {"path": "worker.py", "line": 17, "expr": "acmekit.Thing.create", "via_wrapper": False},
    ]
    return {
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "module-level Thing.create removed; use Client().create",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": "acmekit.Thing.create",
                "old": "acmekit.Thing.create",
                "new": "acmekit.Client.create",
                "new_docstring": "Instance method on Client.",
                "candidates": cand,
                "sites": sites,
            },
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "AcmeError moved from acmekit.errors to acmekit",
                "confidence": "high",
                "source": "wheel",
                "kind": "renamed",
                "symbol": "acmekit.errors.AcmeError",
                "old": "acmekit.errors.AcmeError",
                "new": "acmekit.AcmeError",
                "candidates": [],
                "sites": [
                    {
                        "path": "worker.py",
                        "line": 2,
                        "expr": "acmekit.errors.AcmeError",
                        "via_wrapper": False,
                    }
                ],
            },
        ],
        "deprecations": [],
        "diffed": ["acmekit"],
        "notes": [],
    }


def _migrated_file() -> str:
    return textwrap.dedent(
        """\
        import acmekit
        from acmekit import AcmeError

        client = acmekit.Client()

        def fetch_one(key):
            try:
                return client.create(key)
            except AcmeError:
                return None

        def fetch_two(key):
            try:
                return client.create(key, mode="fast")
            except AcmeError:
                return None

        def ping():
            return client.create("ping")
        """
    )


def _whole_file_reply(source: str) -> str:
    return json.dumps({"file_source": source})


def _fn_edit(name: str, body: str) -> str:
    return json.dumps({"function_source": f"def {name}(key):\n    {body}\n", "new_imports": []})


def test_qualifies_module_to_client_and_moved_error(tmp_path: Path):
    root = _acmekit_repo(tmp_path)
    sites = sites_from_payload(_acmekit_impacts())
    assert qualifies_for_whole_file(root, [s for s in sites if s.path == "worker.py"])


def test_qualifies_three_plus_sharing_sdk_import(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        "import acmekit\n\ndef a():\n    return acmekit.x()\n\ndef b():\n    return acmekit.y()\n\ndef c():\n    return acmekit.z()\n",
        encoding="utf-8",
    )
    payload = {
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1",
                "to_version": "2",
                "change": "x renamed",
                "confidence": "high",
                "source": "wheel",
                "kind": "renamed",
                "symbol": "acmekit.x",
                "old": "x",
                "new": "x2",
                "sites": [
                    {"path": "mod.py", "line": 4, "expr": "acmekit.x"},
                    {"path": "mod.py", "line": 7, "expr": "acmekit.y"},
                    {"path": "mod.py", "line": 10, "expr": "acmekit.z"},
                ],
            }
        ],
    }
    sites = sites_from_payload(payload)
    assert qualifies_for_whole_file(root, sites)


def test_non_qualifying_single_param_rename(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        "def broken(client):\n    return client.old_call(1)\n",
        encoding="utf-8",
    )
    payload = {
        "dependency": "example-sdk",
        "impacts": [
            {
                "dependency": "example-sdk",
                "from_version": "1",
                "to_version": "2",
                "change": "old_call renamed to new_call",
                "confidence": "high",
                "source": "wheel",
                "kind": "param_renamed",
                "symbol": "Client.method",
                "old": "old_call",
                "new": "new_call",
                "sites": [{"path": "mod.py", "line": 2, "expr": "client.old_call"}],
            }
        ],
    }
    sites = sites_from_payload(payload)
    assert not qualifies_for_whole_file(root, sites)


def test_oversized_file_does_not_qualify(tmp_path: Path):
    root = _acmekit_repo(tmp_path)
    lines = (root / "worker.py").read_text(encoding="utf-8").splitlines()
    pad = ["# pad"] * (WHOLE_FILE_MAX_LINES + 1 - len(lines))
    (root / "worker.py").write_text("\n".join(lines + pad) + "\n", encoding="utf-8")
    sites = [s for s in sites_from_payload(_acmekit_impacts()) if s.path == "worker.py"]
    assert not qualifies_for_whole_file(root, sites)


def test_whole_file_prompt_includes_all_sites_and_signatures():
    messages = propose_whole_file_messages(
        file_path="worker.py",
        file_source="import acmekit\n",
        sites=[{"path": "worker.py", "line": 1, "expr": "acmekit.Thing.create"}],
        signatures=[{"symbol": "acmekit.Client.create", "signature": "create(self, key)"}],
        dependency="acmekit",
        from_version="1.0",
        to_version="2.0",
    )
    assert messages[0][0] == "system"
    assert "WHOLE file" in messages[0][1] or "whole file" in messages[0][1].lower()
    user = messages[1][1]
    assert "acmekit.Thing.create" in user
    assert "acmekit.Client.create" in user
    assert "file_source" in user


def test_parse_whole_file_edit_rejects_non_python():
    try:
        parse_whole_file_edit(json.dumps({"file_source": "def missing\n"}))
        assert False, "expected PatchError"
    except PatchError as exc:
        assert "parse" in str(exc).lower()


def test_acmekit_module_to_client_fixed_whole_file(tmp_path: Path):
    root = _acmekit_repo(tmp_path)
    sites = sites_from_payload(_acmekit_impacts())
    migrated = _migrated_file()

    def cleared(_root: Path, site, patched: str) -> bool:
        if "acmekit.Thing.create" in patched:
            return False
        if "acmekit.errors" in patched:
            return False
        if "client = acmekit.Client()" not in patched:
            return False
        if "from acmekit import AcmeError" not in patched:
            return False
        return "client.create" in patched

    client = FakeModelClient(
        responses=[_whole_file_reply(migrated), "Shared client and moved AcmeError."]
    )
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=2),
            write=True,
            client=client,
            site_cleared=cleared,
        ),
    )
    # One whole-file propose (+ summarize), not one propose per site.
    propose_calls = [c for c in client.calls if c["model"] == _settings().model]
    assert len(propose_calls) == 1
    assert "file_source" in propose_calls[0]["turns"][0].content or "WHOLE" in propose_calls[0]["turns"][0].content

    text = (root / "worker.py").read_text(encoding="utf-8")
    assert "client = acmekit.Client()" in text
    assert text.count("acmekit.Client()") == 1
    assert "from acmekit import AcmeError" in text
    assert "acmekit.errors" not in text
    assert "acmekit.Thing.create" not in text
    assert "client.create" in text

    statuses = {result.status for result in report.results}
    assert SiteStatus.NEEDS_HUMAN not in statuses
    assert all(
        result.status in {SiteStatus.FIXED_UNVERIFIED, SiteStatus.FIXED_VERIFIED, SiteStatus.SKIPPED}
        for result in report.results
    )
    fixed = [result for result in report.results if result.status in {SiteStatus.FIXED_UNVERIFIED, SiteStatus.FIXED_VERIFIED}]
    assert len(fixed) >= 3
    assert all(result.diff for result in fixed)


def test_whole_file_failure_falls_back_to_per_site(tmp_path: Path):
    from conduit.apply.context import enclosing_function

    root = _acmekit_repo(tmp_path)
    sites = sites_from_payload(_acmekit_impacts())
    # Only the three create sites (drop the error-import site) so per-site edits are functions.
    create_sites = [site for site in sites if site.expr == "acmekit.Thing.create"]

    bad = _whole_file_reply("def not closed(\n")
    # Per-site fallbacks: one good edit + why for each of the three functions.
    per_site = []
    for name in ("fetch_one", "fetch_two", "ping"):
        if name == "ping":
            per_site.append(
                json.dumps(
                    {
                        "function_source": "def ping():\n    return acmekit.Client().create(\"ping\")\n",
                        "new_imports": [],
                    }
                )
            )
        else:
            per_site.append(
                json.dumps(
                    {
                        "function_source": (
                            f"def {name}(key):\n"
                            f"    try:\n"
                            f"        return acmekit.Client().create(key)\n"
                            f"    except Exception:\n"
                            f"        return None\n"
                        ),
                        "new_imports": [],
                    }
                )
            )
        per_site.append(f"fixed {name}")

    def cleared(_root: Path, site, patched: str) -> bool:
        # Whole-file success: no module-level Thing.create left.
        if "acmekit.Thing.create" not in patched:
            return "create(" in patched
        # Per-site partial file: only the enclosing function must be clear.
        span = enclosing_function(patched, site.line)
        if span is None:
            return False
        return "Thing.create" not in span[1] and "create(" in span[1]

    client = FakeModelClient(responses=[bad, bad, *per_site])
    report = apply_sites(
        create_sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=2),
            write=True,
            client=client,
            site_cleared=cleared,
        ),
    )
    # Whole-file tried (and failed) before per-site proposes.
    assert any("file_source" in (c["turns"][0].content or "") for c in client.calls)
    assert any(
        "one broken call site" in (c["turns"][0].content or "") for c in client.calls
    )
    assert any(result.status == SiteStatus.FIXED_UNVERIFIED for result in report.results)
    text = (root / "worker.py").read_text(encoding="utf-8")
    assert "acmekit.Thing.create" not in text


def test_non_qualifying_file_uses_per_site_prompt(tmp_path: Path):
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
    payload = {
        "dependency": "example-sdk",
        "impacts": [
            {
                "dependency": "example-sdk",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "parameter old_call renamed to new_call on Client",
                "confidence": "high",
                "source": "wheel",
                "kind": "param_renamed",
                "symbol": "Client.method",
                "old": "old_call",
                "new": "new_call",
                "sites": [{"path": "mod.py", "line": 2, "expr": "client.old_call"}],
            }
        ],
    }
    sites = sites_from_payload(payload)
    edit = json.dumps(
        {"function_source": "def broken(client):\n    return client.new_call(1)\n", "new_imports": []}
    )
    client = FakeModelClient(responses=[edit, "renamed keyword"])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            site_cleared=lambda *_a, **_k: True,
        ),
    )
    assert report.results[0].status == SiteStatus.FIXED_UNVERIFIED
    assert "function_source" in client.calls[0]["turns"][0].content or "one broken call site" in client.calls[0]["turns"][0].content
    assert "WHOLE file" not in client.calls[0]["turns"][0].content
    assert "client.new_call" in (root / "mod.py").read_text(encoding="utf-8")


def test_site_still_present_helper():
    site = sites_from_payload(_acmekit_impacts())[0]
    assert site_still_present("return acmekit.Thing.create(key)\n", site)
    assert not site_still_present("return client.create(key)\n", site)
