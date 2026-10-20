"""Offline unit tests for ``conduit apply`` with a fake model client."""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conduit.apply.client import FakeModelClient
from conduit.apply.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_MODEL_SMALL,
    TokenFactorySettings,
)
from conduit.apply.context import build_context_pack
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.splice import PatchError, parse_model_edit, splice_edit
from conduit.main import app


def _repo_with_break(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        textwrap.dedent(
            """\
            import helper

            def other():
                return helper.keep()

            def broken(client):
                return client.old_call(1)

            def also_ok():
                return 2
            """
        ),
        encoding="utf-8",
    )
    return root


def _impact_payload(*, path: str = "mod.py", line: int = 7) -> dict:
    return {
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
                "new_docstring": "Replacement keyword.",
                "sites": [
                    {
                        "path": path,
                        "line": line,
                        "expr": "client.old_call",
                        "confidence": "high",
                        "via_wrapper": False,
                    }
                ],
            }
        ],
        "deprecations": [],
        "diffed": ["example-sdk"],
        "notes": [],
    }


def _good_edit() -> str:
    return json.dumps(
        {
            "function_source": "def broken(client):\n    return client.new_call(1)\n",
            "new_imports": [],
        }
    )


def _bad_edit_wrong_name() -> str:
    return json.dumps(
        {
            "function_source": "def other():\n    return helper.changed()\n",
            "new_imports": [],
        }
    )


def _settings(**kwargs) -> TokenFactorySettings:
    return TokenFactorySettings.from_env(api_key="test-key-not-for-logging", **kwargs)


def test_defaults_match_token_factory_docs():
    settings = TokenFactorySettings.from_env(api_key="")
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.model == DEFAULT_MODEL
    assert settings.model_small == DEFAULT_MODEL_SMALL
    assert "tokenfactory.nebius.com" in settings.base_url
    assert "Nemotron" in settings.model or "nemotron" in settings.model.lower()


def test_dry_run_prints_context_without_calling_model(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    client = FakeModelClient(responses=["should not be used"])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            dry_run=True,
            client=client,
        ),
    )
    assert client.calls == []
    assert report.results[0].status == SiteStatus.DRY_RUN
    pack = report.results[0].context
    assert pack is not None
    assert "old_call" in pack.function_source
    assert pack.release_diff["old"] == "old_call"
    assert pack.release_diff["new"] == "new_call"


def test_happy_path_write_verified(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())

    def cleared(_root: Path, site, patched: str) -> bool:
        return "old_call" not in patched and "new_call" in patched

    client = FakeModelClient(responses=[_good_edit(), "Rename keyword per release diff."])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            test_cmd="python3 -c \"print('ok')\"",
            client=client,
            site_cleared=cleared,
        ),
    )
    assert report.results[0].status == SiteStatus.FIXED_VERIFIED, report.results[0].error
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "new_call" in text
    assert "old_call" not in text
    assert "def other():" in text
    assert report.results[0].diff.startswith("---")


def test_wrong_function_name_rejected_then_retry(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    client = FakeModelClient(
        responses=[_bad_edit_wrong_name(), _good_edit(), "Fixed only the flagged function."]
    )
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
    assert report.results[0].attempts == 2
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "helper.keep()" in text
    assert "new_call" in text


def test_splice_rejects_wrong_function_name(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    site = sites_from_payload(_impact_payload())[0]
    pack = build_context_pack(root, site)
    with pytest.raises(PatchError, match="expected 'broken'"):
        splice_edit(pack, parse_model_edit(_bad_edit_wrong_name()))


def test_splice_adds_imports_without_duplicates(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    site = sites_from_payload(_impact_payload())[0]
    pack = build_context_pack(root, site)
    edit = parse_model_edit(
        json.dumps(
            {
                "function_source": "def broken(client):\n    return client.new_call(1)\n",
                "new_imports": ["import helper", "import os"],
            }
        )
    )
    patched, _diff = splice_edit(pack, edit)
    assert patched.count("import helper") == 1
    assert "import os" in patched


def test_retry_on_test_failure_then_needs_human(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    client = FakeModelClient(responses=[_good_edit(), _good_edit(), _good_edit()])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            write=False,
            test_cmd="python3 -c \"raise SystemExit(1)\"",
            client=client,
            site_cleared=lambda *_a, **_k: True,
        ),
    )
    assert report.results[0].status == SiteStatus.NEEDS_HUMAN
    assert report.results[0].attempts == 3
    assert "old_call" in (root / "mod.py").read_text(encoding="utf-8")


def test_cli_dry_run(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    impact_path = tmp_path / "impact.json"
    impact_path.write_text(json.dumps(_impact_payload()), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["apply", "--path", str(root), "--impact", str(impact_path), "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "dry-run"
    assert payload["context_packs"][0]["function_name"] == "broken"
    assert payload["context_packs"][0]["release_diff"]["new"] == "new_call"


def test_cli_rejects_dry_run_and_write(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    impact_path = tmp_path / "impact.json"
    impact_path.write_text(json.dumps(_impact_payload()), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "apply",
            "--path",
            str(root),
            "--impact",
            str(impact_path),
            "--dry-run",
            "--write",
        ],
    )
    assert result.exit_code == 2


def test_propose_and_summarize_pass_enable_thinking_false_by_default(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    client = FakeModelClient(responses=[_good_edit(), "why"])
    apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            client=client,
            site_cleared=lambda *_a, **_k: True,
        ),
    )
    assert client.calls[0]["enable_thinking"] is False
    assert client.calls[1]["enable_thinking"] is False


def test_enable_thinking_configurable_per_role(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    client = FakeModelClient(responses=[_good_edit(), "why"])
    apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(enable_thinking=True, enable_thinking_small=True),
            client=client,
            site_cleared=lambda *_a, **_k: True,
        ),
    )
    assert client.calls[0]["enable_thinking"] is True
    assert client.calls[1]["enable_thinking"] is True


def test_token_factory_client_sends_thinking_kwarg_and_ignores_reasoning():
    from conduit.apply.client import ChatMessage, TokenFactoryClient

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "reasoning_content": "secret chain of thought",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                "model": "m",
            }

    class _Http:
        def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _Resp()

        def close(self) -> None:
            return None

    client = TokenFactoryClient(base_url="https://example.test/v1/", api_key="secret")
    client._http = _Http()  # type: ignore[assignment]
    result = client.chat(
        model="m",
        messages=[ChatMessage(role="user", content="hi")],
        max_tokens=20,
        enable_thinking=False,
    )
    assert result.content == "ok"
    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_trim_test_feedback_keeps_last_n_lines():
    from conduit.apply.feedback import TEST_FEEDBACK_MAX_LINES, trim_test_feedback

    lines = [f"line-{i}" for i in range(TEST_FEEDBACK_MAX_LINES + 25)]
    trimmed = trim_test_feedback("\n".join(lines))
    assert "line-0" not in trimmed
    assert f"line-{TEST_FEEDBACK_MAX_LINES + 24}" in trimmed
    assert "earlier lines omitted" in trimmed


def test_test_failure_feedback_fed_to_model_is_trimmed(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    huge = "\n".join(f"noise-{i}" for i in range(200))
    log_file = tmp_path / "huge.log"
    log_file.write_text(huge + "\n", encoding="utf-8")
    client = FakeModelClient(responses=[_good_edit(), _good_edit(), "fixed after retry"])
    fail_once = tmp_path / "fail_once.sh"
    marker = tmp_path / "ran"
    fail_once.write_text(
        "#!/bin/sh\n"
        f"if [ ! -f '{marker}' ]; then touch '{marker}'; cat '{log_file}'; exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fail_once.chmod(0o755)
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            client=client,
            test_cmd=str(fail_once),
            site_cleared=lambda *_a, **_k: True,
        ),
    )
    assert report.results[0].status == SiteStatus.FIXED_VERIFIED, report.results[0].error
    second_user = client.calls[1]["messages"][1].content
    assert "noise-0" not in second_user
    assert "earlier lines omitted" in second_user
    assert "noise-199" in second_user


def test_recheck_impact_uses_collect_impacts_on_scratch(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    calls: list[Path] = []

    def fake_listed(scratch: Path, site, *, span, fixtures, allow_network) -> list[tuple[str, str]]:
        calls.append(scratch)
        text = (scratch / site.path).read_text(encoding="utf-8")
        return [("mod.py:7 client.old_call: renamed", "")] if "old_call" in text else []

    monkeypatch.setattr(verify_mod, "_breaks_listed", fake_listed)
    client = FakeModelClient(responses=[_good_edit(), "why"])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            recheck_impact=True,
        ),
    )
    assert report.results[0].status == SiteStatus.FIXED_UNVERIFIED, report.results[0].error
    assert calls
    assert "new_call" in (root / "mod.py").read_text(encoding="utf-8")


def test_recheck_impact_rejects_when_site_still_listed(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    monkeypatch.setattr(verify_mod, "_breaks_listed", lambda *a, **k: [("mod.py:7 client.old_call: x", "")])
    client = FakeModelClient(responses=[_good_edit(), _good_edit(), _good_edit()])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            client=client,
            recheck_impact=True,
        ),
    )
    assert report.results[0].status == SiteStatus.NEEDS_HUMAN
    assert "still breaks" in report.results[0].error


def test_apply_break_fixture_paths_and_evidence():
    fixture = Path(__file__).resolve().parent / "fixtures" / "apply-break"
    assert (fixture / "db.py").is_file()
    assert (fixture / "impact.json").is_file()
    assert (fixture / "test_fetch.py").is_file()
    payload = json.loads((fixture / "impact.json").read_text(encoding="utf-8"))
    sites = sites_from_payload(payload)
    assert sites
    assert sites[0].old_signature == "object_"
    assert sites[0].new_signature == "statement"
    assert sites[0].kind == "param_renamed"
    pack = build_context_pack(fixture, sites[0])
    assert pack.release_diff["old"] == "object_"
    assert pack.release_diff["new"] == "statement"


def test_via_wrapper_sites_are_skipped_not_edited(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    payload = _impact_payload()
    payload["impacts"][0]["sites"].append(
        {
            "path": "mod.py",
            "line": 4,
            "expr": "other",
            "confidence": "high",
            "via_wrapper": True,
        }
    )
    sites = sites_from_payload(payload)
    assert any(site.via_wrapper for site in sites)
    client = FakeModelClient(responses=[_good_edit(), "why"])
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
    skipped = [item for item in report.results if item.status == SiteStatus.SKIPPED]
    assert len(skipped) == 1
    assert "wrapper caller" in skipped[0].why
    fixed = [item for item in report.results if item.status != SiteStatus.SKIPPED]
    assert fixed
    assert "new_call" in (root / "mod.py").read_text(encoding="utf-8")
    # Model only called for the non-wrapper site (propose + summarize).
    assert len(client.calls) == 2


def test_candidates_flow_into_context_pack_and_prompt(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    payload = _impact_payload()
    payload["impacts"][0]["kind"] = "removed"
    payload["impacts"][0]["new"] = ""
    payload["impacts"][0]["symbol"] = "pkg.ChatCompletion.create"
    payload["impacts"][0]["candidates"] = [
        {
            "symbol": "pkg.chat.completions.create",
            "signature": "pkg.chat.completions.create(self, *, messages)",
        }
    ]
    site = sites_from_payload(payload)[0]
    pack = build_context_pack(root, site)
    assert pack.candidates
    assert pack.candidates[0]["symbol"].endswith("create")
    assert pack.release_diff["candidates"][0]["signature"]
    from conduit.apply.prompts import propose_messages

    _system, user = propose_messages(pack)
    assert "candidates" in user[1]
    assert "pkg.chat.completions.create" in user[1]


def test_apply_help_describes_rewrite_not_model_diff():
    result = CliRunner(env={"COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb"}).invoke(
        app, ["apply", "--help"]
    )
    assert result.exit_code == 0
    text = (result.stdout or "").lower()
    assert "rewritten function" in text or "rewrite" in text
    assert "splices" in text or "splice" in text
    # Must not claim the model emits the unified diff.
    assert "asks the model for a function-scoped unified diff" not in text


@pytest.mark.network
@pytest.mark.nebius
def test_live_token_factory_smoke():
    """Live smoke against Token Factory; skipped without NEBIUS_API_KEY."""
    import os

    if not os.environ.get("NEBIUS_API_KEY"):
        pytest.skip("NEBIUS_API_KEY not set")

    from conduit.apply.client import ChatMessage, TokenFactoryClient
    from conduit.apply.config import TokenFactorySettings

    settings = TokenFactorySettings.from_env()
    client = TokenFactoryClient(base_url=settings.base_url, api_key=settings.api_key)
    try:
        result = client.chat(
            model=settings.model_small,
            messages=[ChatMessage(role="user", content="Reply with exactly: ok")],
            max_tokens=16,
            enable_thinking=False,
        )
    finally:
        client.close()
    assert result.content


def test_class_method_rewrite_is_dedented_and_reindented(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        textwrap.dedent(
            """\
            class Wrapper:
                def call(self, client):
                    return client.old_call(1)

                def keep(self):
                    return 2
            """
        ),
        encoding="utf-8",
    )
    site = sites_from_payload(_impact_payload(line=3))[0]
    pack = build_context_pack(root, site)
    # Models return the method as it sits in the class: indented.
    edit = json.dumps(
        {
            "function_source": "    def call(self, client):\n        return client.new_call(1)\n",
            "new_imports": [],
        }
    )
    patched, diff = splice_edit(pack, parse_model_edit(edit))
    assert "    def call(self, client):\n        return client.new_call(1)\n" in patched
    assert "    def keep(self):" in patched
    assert "old_call" not in patched
    assert diff.startswith("---")


def test_module_level_statement_is_rewritten_alone(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        textwrap.dedent(
            """\
            import helper

            TURN = helper.OLD_PROMPT

            def other():
                return TURN
            """
        ),
        encoding="utf-8",
    )
    site = sites_from_payload(_impact_payload(line=3))[0]
    pack = build_context_pack(root, site)
    assert pack.function_name == ""
    assert pack.function_source == "TURN = helper.OLD_PROMPT\n"
    edit = json.dumps({"function_source": 'TURN = "\\n\\nHuman:"\n', "new_imports": []})
    patched, _diff = splice_edit(pack, parse_model_edit(edit))
    assert 'TURN = "\\n\\nHuman:"\n' in patched
    assert "OLD_PROMPT" not in patched
    assert "def other():\n    return TURN\n" in patched


def test_empty_module_statement_rewrite_deletes_it(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text("import os\nfrom helper import Gone\n\nX = os.sep\n", encoding="utf-8")
    site = sites_from_payload(_impact_payload(line=2))[0]
    pack = build_context_pack(root, site)
    edit = json.dumps({"function_source": "", "new_imports": []})
    patched, _diff = splice_edit(pack, parse_model_edit(edit))
    assert patched == "import os\n\nX = os.sep\n"


def _hits_in_span(scratch: Path, site, *, span, fixtures, allow_network) -> list[tuple[str, str]]:
    """Stand-in for impact: a removed ``timeout`` keyword inside the rewritten span."""
    lines = (scratch / site.path).read_text(encoding="utf-8").splitlines()
    return [
        (
            f"{site.path}:{number} client.new_call: parameter timeout removed from Client.new_call",
            "example_sdk.client.Client.new_call",
        )
        for number, line in enumerate(lines, start=1)
        if span[0] <= number <= span[1] and "timeout=" in line
    ]


def test_recheck_rejects_rewrite_that_uses_a_removed_param(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    monkeypatch.setattr(verify_mod, "_breaks_listed", _hits_in_span)
    still_broken = json.dumps(
        {
            "function_source": "def broken(client):\n    return client.new_call(1, timeout=5)\n",
            "new_imports": [],
        }
    )
    client = FakeModelClient(responses=[still_broken, still_broken])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=2),
            write=True,
            test_cmd="python3 -c \"print('ok')\"",
            client=client,
            recheck_impact=True,
        ),
    )
    result = report.results[0]
    assert result.status == SiteStatus.NEEDS_HUMAN
    assert "parameter timeout removed" in result.error
    assert result.attempts == 2
    # The finding went back to the model as the retry reason.
    retry_prompt = client.calls[1]["messages"][-1].content
    assert "parameter timeout removed" in retry_prompt
    assert "old_call" in (root / "mod.py").read_text(encoding="utf-8")


def test_recheck_accepts_clean_rewrite_after_feedback(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    monkeypatch.setattr(verify_mod, "_breaks_listed", _hits_in_span)
    still_broken = json.dumps(
        {
            "function_source": "def broken(client):\n    return client.new_call(1, timeout=5)\n",
            "new_imports": [],
        }
    )
    client = FakeModelClient(responses=[still_broken, _good_edit(), "why"])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            write=True,
            test_cmd="python3 -c \"print('ok')\"",
            client=client,
            recheck_impact=True,
        ),
    )
    result = report.results[0]
    assert result.status == SiteStatus.FIXED_VERIFIED, result.error
    assert result.attempts == 2
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "client.new_call(1)" in text and "timeout" not in text


def _new_release_fixture(tmp_path: Path) -> Path:
    fixtures = tmp_path / "fixtures"
    pkg = fixtures / "new" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .client import Client\n", encoding="utf-8")
    (pkg / "client.py").write_text(
        textwrap.dedent(
            """\
            from typing import Iterable, TypedDict

            class ItemParam(TypedDict):
                name: str
                size: int

            class Client:
                def new_call(self, value: int, *, items: Iterable[ItemParam], retries: int = 2) -> str:
                    return ""

                def loose(self, **kwargs) -> str:
                    return ""

            class Sdk:
                @property
                def client(self) -> "Client":
                    return Client()
            """
        ),
        encoding="utf-8",
    )
    (fixtures / "index.json").write_text(
        json.dumps({"packages": {"example-sdk": {"wheels": {"2.0": "new"}}}}), encoding="utf-8"
    )
    return fixtures


def test_signatures_read_from_new_release_code(tmp_path: Path):
    from conduit.apply.signatures import signatures_for

    fixtures = _new_release_fixture(tmp_path)
    found = signatures_for(fixtures / "new", ["example_sdk.Client.new_call", "example_sdk.Client.gone"])
    assert list(found) == ["example_sdk.Client.new_call"]
    text = found["example_sdk.Client.new_call"]
    assert "Client.new_call(self, value: int, *, items: Iterable[ItemParam], retries: int=2) -> str" in text
    assert "ItemParam(TypedDict): name: str; size: int" in text


def test_recheck_retry_prompt_carries_new_signature(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root = _repo_with_break(tmp_path)
    fixtures = _new_release_fixture(tmp_path)
    payload = _impact_payload()
    payload["impacts"][0]["symbol"] = "example_sdk.Client.new_call"
    sites = sites_from_payload(payload)
    monkeypatch.setattr(verify_mod, "_breaks_listed", _hits_in_span)
    still_broken = json.dumps(
        {
            "function_source": "def broken(client):\n    return client.new_call(1, timeout=5)\n",
            "new_imports": [],
        }
    )
    client = FakeModelClient(responses=[still_broken, _good_edit(), "why"])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(max_attempts=3),
            client=client,
            recheck_impact=True,
            fixtures=fixtures,
        ),
    )
    assert report.results[0].status == SiteStatus.FIXED_UNVERIFIED, report.results[0].error
    first = client.calls[0]["messages"][-1].content
    retry = client.calls[1]["messages"][-1].content
    assert "new_release_signatures" in first
    assert "parameter timeout removed" in retry
    assert "items: Iterable[ItemParam]" in retry
    assert "ItemParam(TypedDict): name: str; size: int" in retry


def test_unknown_keyword_on_new_release_callable_is_a_break(tmp_path: Path):
    from conduit.apply.impact_input import sites_from_payload as load
    from conduit.apply.signatures import unknown_keywords
    from conduit.apply.verify import _unknown_keyword_breaks
    from conduit.outside_in.model import Chain, ServiceMap

    fixtures = _new_release_fixture(tmp_path)
    wheel = fixtures / "new"
    # Followed through a property's return type to the method.
    assert unknown_keywords(wheel, "example_sdk.Sdk.client.new_call", ("items", "timeout")) == (
        "client.new_call",
        ["timeout"],
    )
    assert unknown_keywords(wheel, "example_sdk.Client.loose", ("anything",)) == ("", [])
    assert unknown_keywords(wheel, "example_sdk.Client.gone", ("x",)) == ("", [])

    def chain(line: int, keywords: tuple[str, ...]) -> Chain:
        return Chain(
            service="example-sdk", package="example-sdk", call_rel="mod.py", call_lineno=line,
            call_symbol="broken", call_expr="client.new_call", hops=(),
            sdk_method="example_sdk.Sdk.client.new_call", kind="sdk_call", params=(),
            site_keywords=keywords, response_fields=(), confidence="high", via_wrapper=False,
        )

    service_map = ServiceMap(
        root=str(tmp_path), entries=(), limitations=(), evidence={}, unresolved_calls=(),
        chains=(chain(7, ("items", "timeout")), chain(20, ("bogus",))),
    )
    payload = _impact_payload()
    payload["impacts"][0]["to_version"] = "2.0"
    site = load(payload)[0]
    found = _unknown_keyword_breaks(service_map, site, "mod.py", (6, 8), fixtures, False)
    assert [text for text, _symbol in found] == [
        "mod.py:7 client.new_call: keyword timeout is not a parameter of client.new_call in example-sdk 2.0"
    ]


def _typed_release(tmp_path: Path) -> dict:
    from conduit.apply.signatures import _modules

    pkg = tmp_path / "typed-wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .client import Client\n", encoding="utf-8")
    (pkg / "client.py").write_text(
        textwrap.dedent(
            """\
            from typing import Iterable, Optional, Sequence, TypedDict, Union
            from .models import Reply

            class ItemParam(TypedDict):
                name: str
                size: int

            ItemsParam = Iterable[ItemParam]

            class Client:
                def send(
                    self,
                    *,
                    items: ItemsParam,
                    note: Optional[str] = None,
                    tags: Sequence[str] = (),
                    limit: Union[int, None] = None,
                    extra: "Unknown" = None,
                ) -> Reply:
                    return Reply()

                def open(self) -> "Dynamic":
                    return Dynamic()

            class Dynamic:
                def __getattr__(self, name):
                    return name
            """
        ),
        encoding="utf-8",
    )
    (pkg / "models.py").write_text(
        textwrap.dedent(
            """\
            from typing import Generic, List, TypeVar

            T = TypeVar("T")

            class Base(Generic[T]):
                def to_dict(self) -> dict:
                    return {}

            class Reply(Base[str]):
                text: str
                parts: List[str]
            """
        ),
        encoding="utf-8",
    )
    return _modules(str(tmp_path / "typed-wheel"))


def _chain(path: str, line: int, expr: str, method: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        call_rel=path, call_lineno=line, call_expr=expr, sdk_method=method,
        kind="method_call", via_wrapper=False,
    )


def test_argument_type_check_flags_obvious_mismatches(tmp_path: Path):
    from conduit.apply.typecheck import argument_breaks

    modules = _typed_release(tmp_path)
    source = textwrap.dedent(
        """\
        class Wrapper:
            def run(self, note: str, rows: list, other):
                prompt = self._render(rows)
                self.client.send(items=prompt, note=note, tags=[note], limit="5")
                self.client.send(items=[{"name": "a", "size": 1}], note=other, tags=note, extra=1)
                self.client.send(items=other, limit=3)

            @staticmethod
            def _render(rows) -> str:
                return ""
        """
    )
    tree = ast.parse(source)
    method = "example_sdk.Client.send"
    first = argument_breaks(modules, tree, "w.py", _chain("w.py", 4, "self.client.send", method), "example-sdk", "2.0")
    assert len(first) == 2, first
    assert "items= passes a str" in first[0] and "expects ItemsParam" in first[0]
    assert "ItemParam is a TypedDict: name: str, size: int" in first[0]
    assert 'a list like [{"name": <str>, "size": <int>}, ...]' in first[0]
    assert "prompt = self._render(rows)" in first[0]
    assert "limit= passes a str" in first[1]
    # A list of dicts, an unknown variable, Sequence[str] given a str (a str is a sequence of str),
    # and an annotation the wheel does not define: none flagged.
    second = argument_breaks(modules, tree, "w.py", _chain("w.py", 5, "self.client.send", method), "example-sdk", "2.0")
    assert second == []
    third = argument_breaks(modules, tree, "w.py", _chain("w.py", 6, "self.client.send", method), "example-sdk", "2.0")
    assert third == []
    # A method the new release's class does not define is itself the finding.
    gone = argument_breaks(modules, tree, "w.py", _chain("w.py", 4, "self.client.send", "example_sdk.Client.gone"), "x", "2")
    assert gone == ["w.py:4 self.client.send: Client in x 2 has no gone (its methods: open, send)"]
    # An owner the new release does not define: unknown, nothing flagged.
    assert argument_breaks(modules, tree, "w.py", _chain("w.py", 4, "self.client.send", "example_sdk.Nope.send"), "x", "2") == []
    # A keyword-only parameter passed positionally is a TypeError at call time.
    positional = ast.parse("def f(client, d):\n    client.send(d)\n    client.send(*d)\n")
    found = argument_breaks(modules, positional, "p.py", _chain("p.py", 2, "client.send", method), "x", "2")
    assert len(found) == 1 and "passes 1 positional arguments" in found[0] and "takes 0 (none)" in found[0], found
    assert argument_breaks(modules, positional, "p.py", _chain("p.py", 3, "client.send", method), "x", "2") == []


def test_attribute_check_flags_reads_the_new_return_type_lacks(tmp_path: Path):
    from conduit.apply.typecheck import attribute_breaks

    modules = _typed_release(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    caller = textwrap.dedent(
        """\
        from result import Result

        def call(client):
            reply = client.send(items=[])
            print(reply.text, reply.to_dict(), reply.gone)
            Result.typed(reply)
            return Result.from_reply(reply)

        def dynamic(client):
            handle = client.open()
            return handle.anything
        """
    )
    result = textwrap.dedent(
        """\
        from typing import Any

        class Result:
            @staticmethod
            def from_reply(reply: Any) -> "Result":
                text = getattr(reply, "completion", None) or reply.text
                return Result()

            @staticmethod
            def other(value: int) -> int:
                return value.real

            @staticmethod
            def typed(reply: "example_sdk.client.Dynamic") -> str:
                return reply.text
        """
    )
    (root / "caller.py").write_text(caller, encoding="utf-8")
    (root / "result.py").write_text(result, encoding="utf-8")
    chains = [
        _chain("caller.py", 4, "client.send", "example_sdk.Client.send"),
        _chain("caller.py", 10, "client.open", "example_sdk.Client.open"),
    ]
    local = attribute_breaks(modules, root, "caller.py", caller, (3, 11), chains, "example-sdk", "2.0")
    assert len(local) == 1, local
    assert "reply.gone" in local[0] and "no attribute gone" in local[0]
    assert "fields: text: str, parts: List[str]" in local[0]
    # A parameter typed by its only caller, read through getattr with a literal name.
    passed = attribute_breaks(modules, root, "result.py", result, (4, 16), chains, "example-sdk", "2.0")
    assert len(passed) == 2, passed
    assert "getattr(reply, 'completion', None)" in passed[0] and "no attribute completion" in passed[0]
    # An annotation naming a release class the callers never pass is itself a break.
    assert "annotated Dynamic but callers pass Reply" in passed[1]


def test_unknown_keyword_not_repeated_for_a_removed_parameter():
    from conduit.apply.verify import _merge_keyword_findings

    found = [("a.py:3 c.create: parameter temperature removed from C.create", "s")]
    _merge_keyword_findings(
        found,
        [
            ("a.py:3 c.create: keyword temperature is not a parameter of C.create in x 2", "s"),
            ("a.py:3 c.create: keyword top_k is not a parameter of C.create in x 2", "s"),
            ("a.py:9 c.create: keyword temperature is not a parameter of C.create in x 2", "s"),
        ],
    )
    assert [text.split(": ", 1)[1] for text, _symbol in found] == [
        "parameter temperature removed from C.create",
        "keyword top_k is not a parameter of C.create in x 2",
        "keyword temperature is not a parameter of C.create in x 2",
    ]


def test_retry_feedback_keeps_findings_from_earlier_attempts():
    from conduit.apply.engine import _with_earlier

    message = "rewrite still breaks against the new release:\n- b\nNew release signatures (read from its own code):\nsig"
    out = _with_earlier(message, ["b"], ["a", "b"])
    assert out.startswith("rewrite still breaks against the new release:\n- b\nEarlier attempts also broke (keep these fixed):\n- a\n")
    assert out.endswith("New release signatures (read from its own code):\nsig")
    assert _with_earlier(message, ["b"], ["b"]) == message


def test_attribute_check_narrows_overloads_by_the_call_keywords(tmp_path: Path):
    from conduit.apply.signatures import _modules
    from conduit.apply.typecheck import attribute_breaks

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(
        textwrap.dedent(
            """\
            from typing import Literal, overload

            class Reply:
                text: str

            class Chunks:
                def __iter__(self):
                    return iter(())

            class Client:
                @overload
                def make(self, *, q: str, stream: Literal[False] = False) -> Reply: ...
                @overload
                def make(self, *, q: str, stream: Literal[True]) -> Chunks: ...
                def make(self, *, q: str, stream: bool = False) -> "Reply | Chunks":
                    return Reply()
            """
        ),
        encoding="utf-8",
    )
    modules = _modules(str(tmp_path / "wheel"))
    source = "def f(client):\n    out = client.make(q='x')\n    return out.text, out.gone\n"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "f.py").write_text(source, encoding="utf-8")
    chains = [_chain("f.py", 2, "client.make", "example_sdk.client.Client.make")]
    found = attribute_breaks(modules, root, "f.py", source, (1, 3), chains, "example-sdk", "2.0")
    # Only the non-streaming overload fits: out is a Reply, so .text is fine and .gone is not.
    assert len(found) == 1 and "out is Reply in" in found[0] and "no attribute gone" in found[0], found


def test_prompt_shows_same_file_helpers_read_only():
    from conduit.apply.models import ContextPack, ImpactSite
    from conduit.apply.prompts import same_file_helpers

    source = textwrap.dedent(
        """\
        class A:
            def run(self, rows):
                text = self._render(rows)
                return send(text)

            @staticmethod
            def _render(rows) -> str:
                return "".join(rows)

            def unrelated(self):
                return 1
        """
    )
    site = ImpactSite(path="a.py", line=4, expr="send", dependency="x", from_version="1", to_version="2", change="c", confidence="high", source="wheel")
    pack = ContextPack(
        site=site, file_path="a.py", imports="", function_name="run", function_source="",
        function_start_line=2, function_end_line=4, full_source=source, release_diff={},
        new_signature="", new_docstring="",
    )
    helpers = same_file_helpers(pack)
    assert len(helpers) == 1 and helpers[0].lstrip().startswith("@staticmethod") and "_render" in helpers[0]


def test_missing_member_through_unannotated_property(tmp_path: Path):
    from conduit.apply.signatures import _modules, missing_member, resolve_callable

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .client import Client\n", encoding="utf-8")
    (pkg / "client.py").write_text(
        textwrap.dedent(
            """\
            class UsersClient:
                async def get(self, id: str) -> dict:
                    return {}

            class Client:
                @property
                def users(self):
                    if self._users is None:
                        self._users = UsersClient()
                    return self._users
            """
        ),
        encoding="utf-8",
    )
    modules = _modules(str(tmp_path / "wheel"))
    assert resolve_callable(modules, "example_sdk.Client.users.get") is not None
    assert missing_member(modules, "example_sdk.Client.users.get") is None
    assert missing_member(modules, "example_sdk.Client.users.get_async") == ("UsersClient", ["get"])
