"""A removed constant's old literal reaches the prompt, read from the old release's code."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.signatures import _modules, literal_value

from test_apply import _settings


def _package(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def _old_release(root: Path) -> Path:
    return _package(
        root,
        {
            "example_sdk/__init__.py": """
                from ._client import Client as Client
                from ._consts import GREETING as GREETING
                """,
            "example_sdk/_consts.py": """
                GREETING = "\\n\\nHello:"
                LIMIT: int = 3
                LONG = "x" * 5000
                WIDE = "%s"
                """,
            "example_sdk/_client.py": """
                from . import _consts
                from ._consts import LIMIT

                SEP = WIDE_ALIAS = "::"

                class Client:
                    GREETING = _consts.GREETING
                    LIMIT = LIMIT
                    SEP = SEP
                    COMPUTED = len("abc")
                    def call(self):
                        return 1
                """,
        },
    )


def test_literal_value_follows_class_and_module_references(tmp_path: Path):
    modules = _modules(str(_old_release(tmp_path / "old")))
    assert literal_value(modules, "example_sdk.Client.GREETING") == repr("\n\nHello:")
    assert literal_value(modules, "example_sdk.GREETING") == repr("\n\nHello:")
    assert literal_value(modules, "example_sdk.Client.LIMIT") == "3"
    assert literal_value(modules, "example_sdk._client.Client.SEP") == repr("::")
    # Not a literal, too long, a method, or unknown: nothing is claimed.
    assert literal_value(modules, "example_sdk.Client.COMPUTED") == ""
    assert literal_value(modules, "example_sdk._consts.LONG") == ""
    assert literal_value(modules, "example_sdk.Client.call") == ""
    assert literal_value(modules, "example_sdk.Client.MISSING") == ""
    assert literal_value(modules, "GREETING") == ""


def _fixtures(tmp_path: Path, *, new_greeting: str | None) -> Path:
    fixtures = tmp_path / "fixtures"
    _old_release(fixtures / "old")
    new_files = {
        "example_sdk/__init__.py": "from ._client import Client as Client\n",
        "example_sdk/_client.py": "class Client:\n    def call(self):\n        return 1\n",
    }
    if new_greeting is not None:
        new_files["example_sdk/_client.py"] = (
            f"class Client:\n    GREETING = {new_greeting!r}\n    def call(self):\n        return 1\n"
        )
    _package(fixtures / "new", new_files)
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"example-sdk": {"wheels": {"1.0": "old", "2.0": "new"}}}}), encoding="utf-8"
    )
    return fixtures


def _first_prompt(tmp_path: Path, fixtures: Path, *, recheck: bool = True) -> str:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "mod.py").write_text(
        "from example_sdk import Client\n\n_TURN = Client.GREETING\n", encoding="utf-8"
    )
    payload = {
        "dependency": "example-sdk",
        "impacts": [
            {
                "dependency": "example-sdk",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "Client.GREETING removed",
                "confidence": "high",
                "source": "wheel",
                "kind": "removed",
                "symbol": "example_sdk.Client.GREETING",
                "old": "example_sdk.Client.GREETING",
                "new": "",
                "sites": [{"path": "mod.py", "line": 3, "expr": "Client.GREETING", "confidence": "high"}],
            }
        ],
        "deprecations": [],
        "diffed": ["example-sdk"],
        "notes": [],
    }
    edit = json.dumps({"function_source": '_TURN = "\\n\\nHello:"', "new_imports": []})
    client = FakeModelClient(responses=[edit, "why", edit, "why", edit, "why"])
    apply_sites(
        sites_from_payload(payload),
        ApplyConfig(
            root=root, settings=_settings(max_attempts=1), client=client, recheck_impact=recheck, fixtures=fixtures
        ),
    )
    return client.calls[0]["messages"][-1].content


def test_removed_constant_prompt_carries_its_old_literal(tmp_path: Path):
    prompt = _first_prompt(tmp_path, _fixtures(tmp_path, new_greeting=None))
    assert '"old_value": "example_sdk.Client.GREETING = ' in prompt
    assert "Hello:" in prompt
    assert '"new_value"' not in prompt


def test_changed_constant_prompt_carries_both_literals(tmp_path: Path):
    prompt = _first_prompt(tmp_path, _fixtures(tmp_path, new_greeting="\n\nHi:"))
    assert '"old_value"' in prompt and '"new_value"' in prompt
    assert "Hi:" in prompt


def test_no_literal_without_recheck_or_when_unchanged(tmp_path: Path):
    prompt = _first_prompt(tmp_path / "a", _fixtures(tmp_path / "a", new_greeting="\n\nHello:"))
    assert '"old_value"' not in prompt
    prompt = _first_prompt(tmp_path / "b", _fixtures(tmp_path / "b", new_greeting=None), recheck=False)
    assert '"old_value"' not in prompt
