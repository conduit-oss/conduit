"""Installed-SDK import inventory for apply: ground the prompt, reject invented names.

Uses only a made-up ``acmekit`` package under a tmp dir. No real-world SDKs.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.inventory import (
    IMPORTS_FAILURE,
    MAX_INVENTORY,
    MAX_INVENTORY_TOKENS,
    build_allowlist,
    build_inventory,
    format_inventory_line,
    import_feedback,
    inventory_token_estimate,
    unknown_sdk_imports,
)
from conduit.apply.models import ContextPack, ImpactSite, SiteStatus
from conduit.apply.prompts import propose_messages

from test_apply import _EXERCISE, _settings


def _acmekit(tmp_path: Path) -> Path:
    """Tiny fake SDK: public Client/Widget/send; private helpers excluded via __all__."""
    root = tmp_path / "site-packages"
    pkg = root / "acmekit"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            """\
            from .client import Client
            from .types import Widget
            from ._hidden import secret_helper

            __all__ = ["Client", "Widget", "VERSION"]

            VERSION = "2.0"
            _INTERNAL = 1
            """
        ),
        encoding="utf-8",
    )
    (pkg / "client.py").write_text(
        textwrap.dedent(
            """\
            class Client:
                def send(self, value: int) -> str:
                    return ""

                def _private_method(self) -> None:
                    return None

            def connect() -> Client:
                return Client()

            def _private_fn() -> None:
                return None
            """
        ),
        encoding="utf-8",
    )
    (pkg / "types.py").write_text(
        textwrap.dedent(
            """\
            class Widget:
                pass

            class _PrivateType:
                pass
            """
        ),
        encoding="utf-8",
    )
    (pkg / "_hidden.py").write_text(
        "def secret_helper():\n    return 1\n",
        encoding="utf-8",
    )
    return root


def _big_acmekit(tmp_path: Path, n: int = 500) -> Path:
    """SDK with hundreds of unrelated public exports to prove the per-site cap."""
    root = tmp_path / "site-packages"
    pkg = root / "acmekit"
    pkg.mkdir(parents=True)
    exports = [f"Export{i}" for i in range(n)]
    (pkg / "__init__.py").write_text(
        "from .client import Client\n"
        + "\n".join(f"{name} = {i}" for i, name in enumerate(exports))
        + "\n",
        encoding="utf-8",
    )
    (pkg / "client.py").write_text(
        "class Client:\n    def send(self, value: int) -> str:\n        return ''\n",
        encoding="utf-8",
    )
    (pkg / "other.py").write_text(
        "class Unrelated:\n    pass\n\n" + "\n".join(f"def util_{i}():\n    return {i}\n" for i in range(50)),
        encoding="utf-8",
    )
    return root


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
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "parameter old_call renamed to send on Client",
                "confidence": "high",
                "source": "wheel",
                "kind": "param_renamed",
                "symbol": "acmekit.Client.send",
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
        "diffed": ["acmekit"],
        "notes": [],
    }


def _edit(source: str, imports: list[str] | None = None) -> str:
    return json.dumps({"function_source": source, "new_imports": imports or []})


def _pack(inventory: list[str], allowlist: list[str] | None = None) -> ContextPack:
    return ContextPack(
        site=ImpactSite(
            path="mod.py",
            line=2,
            expr="client.old_call",
            dependency="acmekit",
            from_version="1.0",
            to_version="2.0",
            change="old_call -> send",
            confidence="high",
            source="wheel",
            kind="param_renamed",
            symbol="acmekit.Client.send",
        ),
        file_path="mod.py",
        imports="",
        function_name="broken",
        function_source="def broken(client):\n    return client.old_call(1)\n",
        function_start_line=1,
        function_end_line=2,
        full_source="def broken(client):\n    return client.old_call(1)\n",
        release_diff={},
        new_signature="",
        new_docstring="",
        sdk_inventory=inventory,
        sdk_allowlist=list(allowlist or inventory),
    )


def test_inventory_respects_all_and_skips_private(tmp_path: Path):
    site = _acmekit(tmp_path)
    allowlist = build_allowlist("acmekit", search_path=[site])
    assert "acmekit.Client" in allowlist
    assert "acmekit.Widget" in allowlist
    assert "acmekit.VERSION" in allowlist
    assert "acmekit.client.connect" in allowlist
    assert all(not name.split(".")[-1].startswith("_") for name in allowlist)
    assert "acmekit._hidden" not in allowlist
    assert "acmekit.secret_helper" not in allowlist
    assert "acmekit._INTERNAL" not in allowlist
    assert "acmekit.client._private_fn" not in allowlist

    names = build_inventory(
        "acmekit",
        related=["acmekit.Client.send", "Widget"],
        search_path=[site],
        allowlist=allowlist,
    )
    assert "acmekit.Client" in names
    assert len(names) <= MAX_INVENTORY
    assert inventory_token_estimate(names) <= MAX_INVENTORY_TOKENS
    # Unrelated type module noise stays out when the site is about Client.send.
    assert "acmekit.types.Widget" in names or "acmekit.Widget" in names


def test_inventory_trimmed_to_relevant_names(tmp_path: Path):
    site = _acmekit(tmp_path)
    allowlist = build_allowlist("acmekit", search_path=[site])
    names = build_inventory(
        "acmekit",
        related=["acmekit.client.Client", "old_call"],
        file_imports="from acmekit.client import Client\n",
        search_path=[site],
        allowlist=allowlist,
    )
    assert names
    assert len(names) <= MAX_INVENTORY
    assert any(name.endswith("Client") or name.endswith(".client") for name in names)
    # No related hints → omit prompt inventory entirely.
    assert build_inventory("acmekit", related=[], allowlist=allowlist) == []


def test_big_sdk_yields_at_most_30_names(tmp_path: Path):
    site = _big_acmekit(tmp_path, n=500)
    allowlist = build_allowlist("acmekit", search_path=[site])
    assert len(allowlist) >= 500
    names = build_inventory(
        "acmekit",
        related=["acmekit.Client.send", "client.old_call"],
        search_path=[site],
        allowlist=allowlist,
    )
    assert 1 <= len(names) <= 30
    assert inventory_token_estimate(names) <= MAX_INVENTORY_TOKENS
    assert any("Client" in name for name in names)
    # Mass unrelated constants must not flood the prompt list.
    export_hits = [name for name in names if name.rsplit(".", 1)[-1].startswith("Export")]
    assert len(export_hits) < len(names)


def test_prompt_includes_compact_inventory(tmp_path: Path):
    site = _acmekit(tmp_path)
    inventory = build_inventory(
        "acmekit", related=["acmekit.Client.send"], search_path=[site]
    )
    turns = propose_messages(_pack(inventory))
    system, user = turns[0][1], turns[1][1]
    assert "target_sdk_inventory" in system
    line = format_inventory_line(inventory)
    assert line
    assert line in user
    # Compact one-liner: not pretty-printed inside the indent=2 JSON object.
    assert '"target_sdk_inventory"' not in user
    assert "\n  \"target_sdk_inventory\"" not in user
    # Prompt stays under a fixed bound for this tiny pack + inventory.
    assert len(user) < 2500
    assert inventory_token_estimate(inventory) <= MAX_INVENTORY_TOKENS


def test_prompt_omits_empty_inventory():
    turns = propose_messages(_pack([]))
    user = turns[1][1]
    assert "target_sdk_inventory" not in user


def test_hallucinated_import_triggers_retry_with_suggestions(tmp_path: Path):
    site_pkgs = _acmekit(tmp_path)
    root = _repo(tmp_path)
    allowlist = build_allowlist("acmekit", search_path=[site_pkgs])
    assert allowlist
    bad = _edit(
        "def broken(client):\n    return FakeClient().send(1)\n",
        imports=["from acmekit import FakeClient"],
    )
    good = _edit(
        "def broken(client):\n    return client.send(1)\n",
        imports=["from acmekit import Client"],
    )
    client = FakeModelClient(responses=[bad, good, "uses Client.send"])

    import sys

    sys.path.insert(0, str(site_pkgs))
    try:
        report = apply_sites(
            sites_from_payload(_payload()),
            ApplyConfig(
                root=root,
                settings=_settings(max_attempts=3),
                write=True,
                test_cmd=_EXERCISE,
                client=client,
            ),
        )
    finally:
        sys.path.remove(str(site_pkgs))

    result = report.results[0]
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error, result.why)
    assert len(client.calls) >= 2
    propose_users = [
        turn.content
        for call in client.calls
        for turn in call["turns"]
        if turn.role == "user" and "Previous attempt failed" in turn.content
    ]
    assert propose_users, client.calls
    feedback = propose_users[0]
    assert IMPORTS_FAILURE in feedback
    assert "FakeClient" in feedback
    assert "Client" in feedback


def test_valid_imports_pass_with_no_retry(tmp_path: Path):
    site_pkgs = _acmekit(tmp_path)
    root = _repo(tmp_path)
    good = _edit(
        "def broken(client):\n    return client.send(1)\n",
        imports=["from acmekit import Client"],
    )
    client = FakeModelClient(responses=[good, "uses send"])
    import sys

    sys.path.insert(0, str(site_pkgs))
    try:
        report = apply_sites(
            sites_from_payload(_payload()),
            ApplyConfig(
                root=root,
                settings=_settings(max_attempts=3),
                write=True,
                test_cmd=_EXERCISE,
                client=client,
            ),
        )
    finally:
        sys.path.remove(str(site_pkgs))

    result = report.results[0]
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error)
    assert len(client.calls) == 2


def test_missing_package_falls_back(tmp_path: Path):
    assert build_allowlist("definitely_not_an_installed_sdk_zz") == []
    assert build_inventory("definitely_not_an_installed_sdk_zz", related=["X"]) == []
    assert unknown_sdk_imports(
        "from definitely_not_an_installed_sdk_zz import Invented\n",
        "definitely_not_an_installed_sdk_zz",
        [],
    ) == []
    root = _repo(tmp_path)
    payload = _payload()
    payload["dependency"] = "missing-sdk-zz"
    payload["impacts"][0]["dependency"] = "missing-sdk-zz"
    edit = _edit(
        "def broken(client):\n    return client.send(1)\n",
        imports=["from missing_sdk_zz import Invented"],
    )
    client = FakeModelClient(responses=[edit, "ok"])
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=2),
            write=True,
            test_cmd=_EXERCISE,
            client=client,
        ),
    )
    result = report.results[0]
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error)
    assert IMPORTS_FAILURE not in (result.error or "")


def test_unknown_sdk_imports_and_feedback_helpers(tmp_path: Path):
    site = _acmekit(tmp_path)
    allowlist = build_allowlist("acmekit", search_path=[site])
    inventory = build_inventory(
        "acmekit", related=["acmekit.Client"], allowlist=allowlist
    )
    unknown = unknown_sdk_imports(
        "from acmekit import FakeClient\nfrom acmekit.types import Widget\n",
        "acmekit",
        allowlist,
    )
    assert unknown == ["acmekit.FakeClient"]
    text = import_feedback(
        unknown, inventory, dependency="acmekit", allowlist=allowlist
    )
    assert IMPORTS_FAILURE in text
    assert "FakeClient" in text
    assert "Client" in text or "Widget" in text


def test_imports_failure_after_retries_exhausted(tmp_path: Path):
    site_pkgs = _acmekit(tmp_path)
    root = _repo(tmp_path)
    bad = _edit(
        "def broken(client):\n    return FakeClient().send(1)\n",
        imports=["from acmekit import FakeClient"],
    )
    client = FakeModelClient(responses=[bad, bad, bad])
    import sys

    sys.path.insert(0, str(site_pkgs))
    try:
        report = apply_sites(
            sites_from_payload(_payload()),
            ApplyConfig(
                root=root,
                settings=_settings(max_attempts=2),
                write=True,
                test_cmd=_EXERCISE,
                client=client,
            ),
        )
    finally:
        sys.path.remove(str(site_pkgs))

    result = report.results[0]
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert IMPORTS_FAILURE in (result.error or "")
    assert "FakeClient" in (result.error or "")
    assert "(imports stage)" in (result.error or "")


def test_gate_rejection_survives_token_budget_exhaustion(tmp_path: Path):
    """A gate miss must remain in the needs-human error when the next loop is out of budget."""
    site_pkgs = _acmekit(tmp_path)
    root = _repo(tmp_path)
    bad = _edit(
        "def broken(client):\n    return FakeClient().send(1)\n",
        imports=["from acmekit import FakeClient"],
    )
    # First propose burns the tiny budget; the following loop would overwrite last_error
    # with only "token budget exhausted" unless the gate reason is preserved.
    client = FakeModelClient(responses=[bad, bad, bad])
    import sys

    sys.path.insert(0, str(site_pkgs))
    try:
        report = apply_sites(
            sites_from_payload(_payload()),
            ApplyConfig(
                root=root,
                settings=_settings(max_attempts=3, max_tokens_per_site=80),
                write=True,
                test_cmd=_EXERCISE,
                client=client,
            ),
        )
    finally:
        sys.path.remove(str(site_pkgs))

    result = report.results[0]
    assert result.status == SiteStatus.NEEDS_HUMAN
    error = result.error or ""
    assert IMPORTS_FAILURE in error, error
    assert "FakeClient" in error
    assert "token budget exhausted" in error
