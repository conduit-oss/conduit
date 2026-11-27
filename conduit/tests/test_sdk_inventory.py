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
    build_inventory,
    import_feedback,
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


def test_inventory_respects_all_and_skips_private(tmp_path: Path):
    site = _acmekit(tmp_path)
    names = build_inventory(
        "acmekit",
        related=["acmekit.Client.send", "Widget"],
        search_path=[site],
    )
    assert "acmekit" in names
    assert "acmekit.Client" in names
    assert "acmekit.Widget" in names
    assert "acmekit.VERSION" in names
    assert "acmekit.client" in names
    assert "acmekit.client.Client" in names
    assert "acmekit.client.connect" in names
    assert "acmekit.types.Widget" in names
    assert all(not name.split(".")[-1].startswith("_") for name in names)
    assert "acmekit._hidden" not in names
    assert "acmekit.secret_helper" not in names
    assert "acmekit._INTERNAL" not in names
    assert "acmekit.client._private_fn" not in names
    # __all__ on the package root limits what __init__ exports as public leaves.
    assert "acmekit.Client" in names
    assert len(names) <= 300


def test_prompt_includes_inventory(tmp_path: Path):
    site = _acmekit(tmp_path)
    inventory = build_inventory("acmekit", related=["Client"], search_path=[site])
    pack = ContextPack(
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
    )
    turns = propose_messages(pack)
    system, user = turns[0][1], turns[1][1]
    assert "target_sdk_inventory" in system
    assert "import ONLY" in system or "import only" in system.lower() or "ONLY" in system
    assert "target_sdk_inventory" in user
    assert "acmekit.Client" in user


def test_hallucinated_import_triggers_retry_with_suggestions(tmp_path: Path):
    site_pkgs = _acmekit(tmp_path)
    root = _repo(tmp_path)
    inventory = build_inventory("acmekit", related=["Client", "Widget"], search_path=[site_pkgs])
    assert inventory
    bad = _edit(
        "def broken(client):\n    return FakeClient().send(1)\n",
        imports=["from acmekit import FakeClient"],
    )
    good = _edit(
        "def broken(client):\n    return client.send(1)\n",
        imports=["from acmekit import Client"],
    )
    client = FakeModelClient(responses=[bad, good, "uses Client.send"])

    # Inject inventory via a thin wrapper: monkeypatch build path by putting
    # acmekit on sys.path so find_spec finds it during apply.
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
    # The retry prompt is a propose call whose user turn carries import feedback.
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
    assert "Client" in feedback  # difflib suggestion


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
    # One propose + one summarize; no import-retry propose.
    assert len(client.calls) == 2


def test_missing_package_falls_back(tmp_path: Path):
    assert build_inventory("definitely_not_an_installed_sdk_zz", related=["X"]) == []
    # Gate off: invented imports are not checked when inventory is empty.
    assert unknown_sdk_imports(
        "from definitely_not_an_installed_sdk_zz import Invented\n",
        "definitely_not_an_installed_sdk_zz",
        [],
    ) == []
    root = _repo(tmp_path)
    # Dependency name that is not installed: apply must not fail the imports gate.
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
    inventory = build_inventory("acmekit", search_path=[site])
    unknown = unknown_sdk_imports(
        "from acmekit import FakeClient\nfrom acmekit.types import Widget\n",
        "acmekit",
        inventory,
    )
    assert unknown == ["acmekit.FakeClient"]
    text = import_feedback(unknown, inventory, dependency="acmekit")
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
