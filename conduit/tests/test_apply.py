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
        turns=[ChatMessage(role="user", content="hi")],
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
    # Batch mode: propose + summary, the whole-tree test run fails once, then one retry.
    client = FakeModelClient(responses=[_good_edit(), "why", _good_edit(), "fixed after retry"])
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
    second_user = client.calls[2]["turns"][1].content
    assert "noise-0" not in second_user
    assert "earlier lines omitted" in second_user
    assert "noise-199" in second_user


def test_recheck_impact_uses_collect_impacts_on_scratch(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    calls: list[Path] = []

    def fake_listed(scratch: Path, site, *, span, fixtures, allow_network, original="") -> list[tuple[str, str]]:
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
            "signature": "pkg.chat.completions.create(self, *, notes)",
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
            turns=[ChatMessage(role="user", content="Reply with exactly: ok")],
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


def _sent(client: FakeModelClient, call: int) -> str:
    """Text of the last turn the fake client received on the given call."""
    return client.calls[call]["turns"][-1].content


def _hits_in_span(scratch: Path, site, *, span, fixtures, allow_network, original="") -> list[tuple[str, str]]:
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
    # A report cell keeps the first line, so the finding has to be on it.
    first = result.error.splitlines()[0]
    assert first.startswith("rewrite still breaks against the new release: ")
    assert "parameter timeout removed" in first
    # The finding went back to the model as the retry reason.
    retry_prompt = client.calls[1]["turns"][-1].content
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
    first = client.calls[0]["turns"][-1].content
    retry = client.calls[1]["turns"][-1].content
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
    assert len(local) == 2, local
    assert "reply.gone" in local[0] and "no attribute gone" in local[0]
    assert "fields: text: str, parts: List[str]" in local[0]
    # The helper this function hands the release value to reads a missing
    # attribute through getattr with a default: flagged from the caller's span.
    assert local[1].startswith("result.py:6 getattr(reply, 'completion', None)"), local[1]
    assert "from_reply() gets reply = Reply" in local[1] and "no attribute completion" in local[1]
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


def test_one_rewrite_per_function_and_later_sites_follow_shifted_lines(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    payload = _impact_payload(line=7)
    extra = [dict(payload["impacts"][0]["sites"][0], line=line) for line in (6, 10)]
    payload["impacts"][0]["sites"].extend(extra)
    sites = sites_from_payload(payload)
    longer = json.dumps(
        {"function_source": "def broken(client):\n    value = client.new_call(1)\n    return value\n", "new_imports": []}
    )
    seen: list[int] = []

    def cleared(_root: Path, site, patched: str) -> bool:
        seen.append(site.line)
        return True

    also = json.dumps({"function_source": "def also_ok():\n    return 3\n", "new_imports": []})
    client = FakeModelClient(responses=[longer, "why", also, "why"])
    report = apply_sites(
        sites, ApplyConfig(root=root, settings=_settings(), write=True, client=client, site_cleared=cleared)
    )
    statuses = [result.status for result in report.results]
    assert statuses[1] == SiteStatus.SKIPPED and "same function as mod.py:7" in report.results[1].why
    assert seen == [7, 11], report.results[2].error  # also_ok's "return 2" moved down one line
    assert "return 3" in (root / "mod.py").read_text(encoding="utf-8")


def test_missing_member_refuses_an_ambiguous_class_guess(tmp_path: Path):
    from conduit.apply.signatures import _modules, missing_member

    pkg = tmp_path / "wheel" / "sdk"
    for sub, method in (("chat", "create"), ("beta", "stream")):
        (pkg / sub).mkdir(parents=True)
        (pkg / sub / "__init__.py").write_text("", encoding="utf-8")
        (pkg / sub / "chat.py").write_text(
            f"class Completions:\n    def {method}(self):\n        pass\n\nclass Chat:\n    completions: Completions\n",
            encoding="utf-8",
        )
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "_client.py").write_text("from . import chat as resources\n\nclass Client:\n    chat: resources.Chat\n", encoding="utf-8")
    modules = _modules(str(tmp_path / "wheel"))
    # Chat is defined twice at the same distance from the client: no guess, no false "has no create".
    assert missing_member(modules, "sdk._client.Client.chat.completions.create") is None
    assert missing_member(modules, "sdk._client.Client.chat.completions.stream") is None


def _two_file_repo(tmp_path: Path) -> tuple[Path, list]:
    root = _repo_with_break(tmp_path)
    (root / "two.py").write_text("def second(client):\n    return client.old_call(2)\n", encoding="utf-8")
    payload = _impact_payload(line=7)
    payload["impacts"][0]["sites"].append(dict(payload["impacts"][0]["sites"][0], path="two.py", line=2))
    return root, sites_from_payload(payload)


def _edit(name: str, body: str) -> str:
    return json.dumps({"function_source": f"def {name}(client):\n    return {body}\n", "new_imports": []})


def test_batch_runs_tests_once_after_every_site_is_rewritten(tmp_path: Path):
    root, sites = _two_file_repo(tmp_path)
    # Passes only when neither file still has the old call: each fix alone fails.
    check = "python3 -c \"import pathlib,sys; sys.exit(any('old_call' in pathlib.Path(p).read_text() for p in ('mod.py','two.py')))\""
    client = FakeModelClient(responses=[_edit("broken", "client.new_call(1)"), "why", _edit("second", "client.new_call(2)"), "why"])
    report = apply_sites(
        sites,
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd=check, client=client, site_cleared=lambda *_a: True),
    )
    assert [r.status for r in report.results] == [SiteStatus.FIXED_VERIFIED, SiteStatus.FIXED_VERIFIED]
    assert "old_call" not in (root / "mod.py").read_text() + (root / "two.py").read_text()


def test_batch_retries_the_blamed_site_then_reverts_it(tmp_path: Path):
    root, sites = _two_file_repo(tmp_path)
    # Fails while two.py calls bad_call, naming its file and line like a traceback.
    script = tmp_path / "check.py"
    script.write_text(
        "import pathlib, sys\n"
        "lines = pathlib.Path('two.py').read_text().splitlines()\n"
        "bad = [n for n, line in enumerate(lines, 1) if 'bad_call' in line]\n"
        "if bad:\n"
        "    print(f'  File \"two.py\", line {bad[0]}, in second')\n"
        "    print(\"AttributeError: 'Client' object has no attribute 'bad_call'\")\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    client = FakeModelClient(
        responses=[_edit("broken", "client.new_call(1)"), "why", _edit("second", "client.bad_call(2)"), "why",
                   _edit("second", "client.bad_call(3)"), "why", _edit("second", "client.bad_call(4)"), "why"]
    )
    report = apply_sites(
        sites,
        ApplyConfig(root=root, settings=_settings(max_attempts=3), write=True, test_cmd=f"python3 {script}", client=client,
                    site_cleared=lambda *_a: True),
    )
    first, second = report.results
    assert first.status == SiteStatus.FIXED_VERIFIED
    assert second.status == SiteStatus.NEEDS_HUMAN and second.attempts == 3 and "reverted" in second.error
    assert "bad_call" in client.calls[4]["turns"][1].content  # the retry saw its rewrite and the failure
    assert "new_call(1)" in (root / "mod.py").read_text() and "old_call(2)" in (root / "two.py").read_text()


def test_dropped_argument_the_new_target_still_accepts_is_flagged(tmp_path: Path):
    from types import SimpleNamespace

    from conduit.apply.typecheck import dropped_arguments, original_arguments

    modules = _typed_release(tmp_path)
    before = "def run(client, rows):\n    return client.old_send(rows, note='n', limit=3, retries=2)\n"
    old = _typed_release(tmp_path / "old")
    # The old method is not in this wheel, so only keywords count; still enough.
    site = SimpleNamespace(line=2, expr="client.old_send", symbol="example_sdk.Client.old_send")
    passed = original_arguments(before, site, old)
    assert passed == {"note", "limit", "retries"}
    method = "example_sdk.Client.send"
    after = ast.parse("def run(client, rows):\n    return client.send(items=rows, note='n')\n")
    found = dropped_arguments(modules, after, "w.py", _chain("w.py", 2, "client.send", method), passed, "example-sdk", "2.0")
    # limit is still accepted and no longer passed; retries is gone from the new method, so not demanded.
    assert found == ["w.py:2 client.send: rewrite drops limit which Client.send in example-sdk 2.0 still accepts; keep passing it"]
    kept = ast.parse("def run(client, rows):\n    return client.send(items=rows, note='n', limit=None)\n")
    assert dropped_arguments(modules, kept, "w.py", _chain("w.py", 2, "client.send", method), passed, "example-sdk", "2.0") == []
    # A new call sharing none of the original arguments is a different call, not compared.
    other = ast.parse("def run(client, rows):\n    return client.send(items=rows)\n")
    assert dropped_arguments(modules, other, "w.py", _chain("w.py", 2, "client.send", method), passed, "example-sdk", "2.0") == []


def test_original_positionals_are_named_through_the_old_signature(tmp_path: Path):
    from types import SimpleNamespace

    from conduit.apply.typecheck import original_arguments

    old = _typed_release(tmp_path)
    before = "def run(client):\n    return client.send(1, 2, 3)\n"
    site = SimpleNamespace(line=2, expr="client.send", symbol="example_sdk.Client.send")
    # send takes keyword-only arguments; positionals have no name to map.
    assert original_arguments(before, site, old) == set()
    before = "def run(client):\n    return client.open(**kw)\n"
    site = SimpleNamespace(line=2, expr="client.open", symbol="example_sdk.Client.open")
    assert original_arguments(before, site, old) == set()


def _masked_read_repo(tmp_path: Path) -> tuple[Path, list]:
    """A helper rewrite reads an attribute; a later site passes a new-release value into it."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "helper.py").write_text(
        "def from_completion(result):\n    return result.completion\n",
        encoding="utf-8",
    )
    (root / "caller.py").write_text(
        "def post(client):\n    result = client.old_call(1)\n    return from_completion(result)\n",
        encoding="utf-8",
    )
    payload = _impact_payload(path="helper.py", line=2)
    payload["impacts"][0]["sites"][0]["expr"] = "result.completion"
    payload["impacts"][0]["sites"].append(
        {
            "path": "caller.py",
            "line": 2,
            "expr": "client.old_call",
            "confidence": "high",
            "via_wrapper": False,
        }
    )
    return root, sites_from_payload(payload)


def _fn(source: str) -> str:
    return json.dumps({"function_source": source, "new_imports": []})


_MASKED = "def from_completion(result):\n    return getattr(result, 'completion', str(result))\n"
_READ_TEXT = "def from_completion(result):\n    return result.content[0].text\n"
_CALLER_EDIT = "def post(client):\n    result = client.new_call(1)\n    return from_completion(result)\n"
_MASKED_EXPR = "getattr(result, 'completion', str(result))"


def _masked_finding(scratch: Path) -> str:
    helper = (scratch / "helper.py").read_text(encoding="utf-8")
    line = next(number for number, row in enumerate(helper.splitlines(), 1) if _MASKED_EXPR in row)
    return (
        f"helper.py:{line} {_MASKED_EXPR}: from_completion() gets result = BetaMessage "
        "from caller.py:2 in example-sdk 2.0, which has no attribute completion (fields: content)"
    )


def _masked_breaks(scratch: Path, site, *, span, fixtures, allow_network, original=""):
    """The caller's recheck flags the masked read in the helper. The helper's own recheck stays quiet."""
    helper = (scratch / "helper.py").read_text(encoding="utf-8")
    caller = (scratch / "caller.py").read_text(encoding="utf-8")
    if site.path.endswith("caller.py") and _MASKED_EXPR in helper and "new_call" in caller:
        return [(_masked_finding(scratch), "")]
    return []


def test_batch_routes_masked_read_back_to_the_feeding_rewrite(tmp_path: Path, monkeypatch):
    """The caller feeds the helper, so it is rewritten first; the masked read its recheck
    finds in the helper goes into the helper's prompt instead of failing the caller."""
    from conduit.apply import verify as verify_mod

    root, sites = _masked_read_repo(tmp_path)
    monkeypatch.setattr(verify_mod, "_breaks_listed", _masked_breaks)
    client = FakeModelClient(
        responses=[_fn(_CALLER_EDIT), "why", _fn(_MASKED), "why", _fn(_READ_TEXT), "why"]
    )
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            test_cmd="python3 -c \"print('ok')\"",
            client=client,
            recheck_impact=True,
        ),
    )
    helper, caller = report.results
    assert helper.site.path == "helper.py" and caller.site.path == "caller.py"
    assert helper.status == SiteStatus.FIXED_VERIFIED, helper.error
    assert caller.status == SiteStatus.FIXED_VERIFIED, caller.error
    assert "result.content[0].text" in (root / "helper.py").read_text(encoding="utf-8")
    assert _MASKED_EXPR not in (root / "helper.py").read_text(encoding="utf-8")
    # The helper's first prompt already carries nothing about the masked read
    # (it was not written yet); its retry carries the routed finding.
    retry = client.calls[4]["turns"][-1].content
    assert "no attribute completion" in retry
    assert "BetaMessage" in retry
    assert _MASKED_EXPR in retry


def test_routed_masked_read_is_not_verified_and_names_the_finding(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root, sites = _masked_read_repo(tmp_path)
    monkeypatch.setattr(verify_mod, "_breaks_listed", _masked_breaks)
    # The helper keeps the masked read, and the helper's own recheck does not repeat it.
    client = FakeModelClient(
        responses=[_fn(_CALLER_EDIT), "why", _fn(_MASKED), "why", _fn(_MASKED), "why"]
    )
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            test_cmd="python3 -c \"print('ok')\"",
            client=client,
            recheck_impact=True,
        ),
    )
    helper, caller = report.results
    assert caller.status == SiteStatus.FIXED_VERIFIED, caller.error
    assert helper.status == SiteStatus.NEEDS_HUMAN
    first = helper.error.splitlines()[0]
    assert first.startswith("rewrite still breaks against the new release: ")
    assert "no attribute completion" in first
    assert _MASKED_EXPR in first
    assert _MASKED_EXPR not in (root / "helper.py").read_text(encoding="utf-8")
    assert "new_call" in (root / "caller.py").read_text(encoding="utf-8")


def test_hasattr_on_a_release_value_without_the_attribute_is_flagged(tmp_path: Path):
    from conduit.apply.typecheck import attribute_breaks

    modules = _typed_release(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    caller = "def call(client):\n    reply = client.send(items=[])\n    return reply.text if hasattr(reply, 'gone') else None\n"
    (root / "caller.py").write_text(caller, encoding="utf-8")
    chains = [_chain("caller.py", 2, "client.send", "example_sdk.Client.send")]
    found = attribute_breaks(modules, root, "caller.py", caller, (1, 3), chains, "example-sdk", "2.0")
    assert len(found) == 1 and "no attribute gone" in found[0], found


def test_dropped_argument_on_a_nested_overload_is_still_flagged(tmp_path: Path):
    """Keyword-only names on an overload still count when the implementation takes ``**kwargs``
    and the callee is a nested resource returned as ``return Cls(self)`` with no annotation."""
    from conduit.apply.signatures import _modules
    from conduit.apply.typecheck import dropped_arguments

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .client import Client\n", encoding="utf-8")
    (pkg / "client.py").write_text(
        textwrap.dedent(
            """\
            from typing import Literal, overload

            class Reply:
                content: str

            class Notes:
                @overload
                def create(
                    self, *, model: str, max_tokens: int, temperature: float = 1,
                    top_k: int = 1, top_p: float = 1, stream: Literal[False] = False,
                ) -> Reply: ...
                @overload
                def create(
                    self, *, model: str, max_tokens: int, temperature: float = 1,
                    top_k: int = 1, top_p: float = 1, stream: Literal[True],
                ) -> Reply: ...
                def create(self, *, model: str, max_tokens: int, stream: bool = False, **kwargs) -> Reply:
                    return Reply()

            class Beta:
                @property
                def notes(self):
                    return Notes(self)

            class Client:
                @property
                def beta(self):
                    return Beta(self)
            """
        ),
        encoding="utf-8",
    )
    modules = _modules(str(tmp_path / "wheel"))
    method = "example_sdk.client.Client.beta.notes.create"
    chain = _chain("w.py", 2, "client.beta.notes.create", method)
    passed = {"model", "max_tokens", "temperature", "top_k", "top_p"}
    dropped = ast.parse(
        "def run(client):\n    return client.beta.notes.create(model='m', max_tokens=1)\n"
    )
    found = dropped_arguments(modules, dropped, "w.py", chain, passed, "example-sdk", "2.0")
    assert found == [
        "w.py:2 client.beta.notes.create: rewrite drops temperature which notes.create in example-sdk 2.0 still accepts; keep passing it",
        "w.py:2 client.beta.notes.create: rewrite drops top_k which notes.create in example-sdk 2.0 still accepts; keep passing it",
        "w.py:2 client.beta.notes.create: rewrite drops top_p which notes.create in example-sdk 2.0 still accepts; keep passing it",
    ], found
    kept = ast.parse(
        "def run(client):\n    return client.beta.notes.create(model='m', max_tokens=1, temperature=0, top_k=1, top_p=1)\n"
    )
    assert dropped_arguments(modules, kept, "w.py", chain, passed, "example-sdk", "2.0") == []


def test_loosened_any_hint_uses_the_callers_type(tmp_path: Path):
    """A hint widened to Any still checks attribute reads against the type the rewritten caller passes.
    When no rewritten site calls the function, the why text says so."""
    import sys

    from conduit.apply.typecheck import attribute_breaks

    ext = tmp_path / "extbase.py"
    ext.write_text(
        "class Model:\n    def __getattr__(self, name):\n        raise AttributeError(name)\n",
        encoding="utf-8",
    )
    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(
        textwrap.dedent(
            """\
            from extbase import Model

            class Reply(Model):
                content: str

            class Notes:
                def create(self, *, model: str) -> Reply:
                    return Reply()

            class Beta:
                @property
                def notes(self):
                    return Notes(self)

            class Client:
                @property
                def beta(self):
                    return Beta(self)
            """
        ),
        encoding="utf-8",
    )
    from conduit.apply.signatures import _modules

    sys.path.insert(0, str(tmp_path))
    sys.modules.pop("extbase", None)
    try:
        modules = _modules(str(tmp_path / "wheel"))
        root = tmp_path / "repo"
        root.mkdir()
        helper = "from typing import Any\n\ndef from_completion(result: Any):\n    return result.completion\n"
        caller = "def post(client):\n    result = client.beta.notes.create(model='m')\n    return from_completion(result)\n"
        (root / "helper.py").write_text(helper, encoding="utf-8")
        (root / "caller.py").write_text(caller, encoding="utf-8")
        chains = [
            _chain("caller.py", 2, "client.beta.notes.create", "example_sdk.client.Client.beta.notes.create")
        ]
        found = attribute_breaks(modules, root, "helper.py", helper, (1, 4), chains, "example-sdk", "2.0")
        assert len(found) == 1 and "result.completion" in found[0] and "no attribute completion" in found[0], found
        assert "Reply" in found[0]
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("extbase", None)

    root = tmp_path / "app"
    root.mkdir()
    (root / "helper.py").write_text(
        "def from_completion(result):\n    return result.completion\n",
        encoding="utf-8",
    )
    (root / "caller.py").write_text(
        "def post(client):\n    result = client.old_call(1)\n    return from_completion(result)\n",
        encoding="utf-8",
    )
    payload = _impact_payload(path="helper.py", line=2)
    payload["impacts"][0]["sites"][0]["expr"] = "result.completion"
    payload["impacts"][0]["sites"].append(
        {"path": "caller.py", "line": 2, "expr": "client.old_call", "confidence": "high", "via_wrapper": False}
    )
    sites = sites_from_payload(payload)
    helper_edit = _fn("def from_completion(result: Any):\n    return result.completion\n")
    caller_edit = _fn("def post(client):\n    result = client.new_call(1)\n    return result.content\n")
    client = FakeModelClient(
        responses=[caller_edit, "switched the call", helper_edit, "Widened the hint, which fixes the attribute."]
    )
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            test_cmd="python3 -c \"print('ok')\"",
            client=client,
            site_cleared=lambda *_a: True,
        ),
    )
    helper, caller_result = report.results
    assert helper.status == SiteStatus.FIXED_VERIFIED, helper.error
    assert helper.why == "no rewritten site calls this function"
    assert "fixes the attribute" not in helper.why
    assert caller_result.status == SiteStatus.FIXED_VERIFIED
    assert caller_result.why == "switched the call"


_UNION_WHEEL = """\
from typing import Annotated, List, Union

class Meta:
    pass

class TextBlock:
    text: str

class ToolBlock:
    id: str

Block = Annotated[Union[TextBlock, ToolBlock], Meta()]

class Reply:
    content: List[Block]

class Notes:
    def create(self, *, model: str) -> Reply:
        return Reply()

class Client:
    @property
    def notes(self):
        return Notes(self)
"""


def test_masked_defaults_on_declared_types(tmp_path: Path):
    """getattr default over union items that lack the field, and a hasattr on a field
    the type always declares, are findings: the fallback hides a wrong value."""
    from conduit.apply.signatures import _modules
    from conduit.apply.typecheck import attribute_breaks

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(_UNION_WHEEL, encoding="utf-8")
    modules = _modules(str(tmp_path / "wheel"))
    root = tmp_path / "repo"
    root.mkdir()
    source = textwrap.dedent(
        """\
        def post(client):
            result = client.notes.create(model='m')
            if hasattr(result, 'content') and result.content:
                return "".join(getattr(block, 'text', str(block)) for block in result.content)
            return str(result)
        """
    )
    (root / "f.py").write_text(source, encoding="utf-8")
    chains = [_chain("f.py", 2, "client.notes.create", "example_sdk.client.Client.notes.create")]
    found = attribute_breaks(modules, root, "f.py", source, (1, 5), chains, "example-sdk", "2.0")
    assert any("hasattr(result, 'content')" in text and "always has content" in text for text in found), found
    assert any("getattr(block, 'text'" in text and "ToolBlock" in text and "TextBlock" not in text.split(" may be ")[1] for text in found), found
    plain = source.replace("getattr(block, 'text', str(block))", "block.text").replace("hasattr(result, 'content') and ", "")
    assert not [t for t in attribute_breaks(modules, root, "f.py", plain, (1, 5), chains, "example-sdk", "2.0") if "TextBlock" in t and "always" in t]


def test_dropped_argument_silent_when_release_removed_it(tmp_path: Path):
    """An argument the new callee no longer declares is not a drop finding."""
    import ast

    from conduit.apply.signatures import _modules
    from conduit.apply.typecheck import dropped_arguments

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(_UNION_WHEEL, encoding="utf-8")
    modules = _modules(str(tmp_path / "wheel"))
    tree = ast.parse("def post(client):\n    return client.notes.create(model='m')\n")
    chain = _chain("f.py", 2, "client.notes.create", "example_sdk.client.Client.notes.create")
    assert dropped_arguments(modules, tree, "f.py", chain, {"model", "temperature"}, "example-sdk", "2.0") == []
    tree = ast.parse("def post(client):\n    return client.notes.create()\n")
    assert dropped_arguments(modules, tree, "f.py", chain, {"model"}, "example-sdk", "2.0") == []  # nothing kept: unrelated call


def test_final_recheck_blocks_verified_after_tests_pass(tmp_path: Path, monkeypatch):
    """A finding that only appears once the whole migration is in place (a caller's new
    type flowing into an Any parameter) keeps the site from verified, even with passing tests."""
    import conduit.apply.engine as engine

    def fake(root, site, patched, *, span=None, fixtures=None, allow_network=False, original=None):
        if original is None or site.path != "helper.py":
            return []
        return [("helper.py:2 result.completion: result is Reply in example-sdk 2.0, which has no attribute completion", "")]

    monkeypatch.setattr(engine, "remaining_breaks", fake)
    root = tmp_path / "app"
    root.mkdir()
    (root / "helper.py").write_text("def from_completion(result):\n    return result.completion\n", encoding="utf-8")
    (root / "caller.py").write_text(
        "def post(client):\n    result = client.old_call(1)\n    return from_completion(result)\n", encoding="utf-8"
    )
    payload = _impact_payload(path="helper.py", line=2)
    payload["impacts"][0]["sites"][0]["expr"] = "result.completion"
    payload["impacts"][0]["sites"].append(
        {"path": "caller.py", "line": 2, "expr": "client.old_call", "confidence": "high", "via_wrapper": False}
    )
    client = FakeModelClient(
        responses=[
            _fn("def post(client):\n    result = client.new_call(1)\n    return from_completion(result)\n"), "switched",
            _fn("def from_completion(result: Any):\n    return result.completion\n"), "widened",
            # the one retry on the final-recheck finding, still reading the old attribute
            _fn("def from_completion(result: Any):\n    return result.completion\n"), "widened again",
        ]
    )
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    helper, caller = report.results
    assert helper.status == SiteStatus.NEEDS_HUMAN, (helper.status, helper.error)
    assert "no attribute completion" in helper.error
    assert helper.why == "Line 2 reads `.completion`, which `Reply` doesn't have in 2.0."
    assert caller.status == SiteStatus.FIXED_VERIFIED


#: council-test (a council fork) before its anthropic 0.34.2 -> 1.0.0 bump, and the
#: impact Conduit mapped for that bump at this commit.
_COUNCIL_REPO = "https://github.com/conduit-oss/council-test.git"
_COUNCIL_SHA = "7dba3aac2a97b5b2ea9848d6989b6d2af1f23359"
_COUNCIL_IMPACT = Path(__file__).resolve().parents[1] / "fixtures" / "apply" / "council-anthropic-0.34.2-1.0.0-impact.json"


def _council_python(tmp_path: Path, repo: Path) -> str:
    """An interpreter with council's requirements and anthropic 1.0.0.

    ``CONDUIT_COUNCIL_PYTHON`` names one; otherwise a venv is built once and
    kept under ``~/.cache/conduit/`` (keyed by the pinned commit)."""
    import os
    import subprocess
    import sys

    given = os.environ.get("CONDUIT_COUNCIL_PYTHON", "").strip()
    if given:
        return given
    env_dir = Path.home() / ".cache" / "conduit" / f"council-env-{_COUNCIL_SHA[:12]}"
    python = env_dir / "bin" / "python"
    if not (env_dir / ".ready").is_file():
        reqs = (repo / "requirements.txt").read_text(encoding="utf-8")
        bumped = "\n".join(
            "anthropic==1.0.0" if line.strip().lower().startswith("anthropic") else line for line in reqs.splitlines()
        )
        pinned = tmp_path / "requirements-1.0.0.txt"
        pinned.write_text(bumped + "\n", encoding="utf-8")
        subprocess.run([sys.executable, "-m", "venv", "--clear", str(env_dir)], check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "-r", str(pinned), "pytest==7.4.4"], check=True, cwd=repo
        )
        (env_dir / ".ready").write_text("", encoding="utf-8")
    return str(python)


def _enclosing_span(source: str, line: int) -> tuple[str, tuple[int, int]] | None:
    """Name and span of the innermost function around ``line``."""
    best = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            if start <= line <= (node.end_lineno or node.lineno):
                if best is None or start >= best[1][0]:
                    best = (node.name, (start, node.end_lineno or node.lineno))
    return best


def _function_span(source: str, name: str) -> tuple[int, int] | None:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return min([node.lineno, *(d.lineno for d in node.decorator_list)]), node.end_lineno or node.lineno
    return None


def _call_keywords(source: str, span: tuple[int, int], attr: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
            and span[0] <= node.lineno <= span[1]
        ):
            found |= {kw.arg for kw in node.keywords if kw.arg}
    return found


@pytest.mark.network
def test_council_anthropic_1_0_bump_is_fixed_or_honestly_flagged(tmp_path: Path):
    """Demo regression, end to end against Token Factory: council-test's anthropic
    0.34.2 -> 1.0.0 sites. The model may fix a line or not, so this checks what must
    hold either way: a line is "fixed (verified)" only when Conduit's own checks pass
    on the written file and the tests show nothing new failing; anything not fixed
    says why; dropped arguments are reported; the wrapper caller is skipped."""
    import os
    import shlex
    import shutil
    import subprocess

    from conduit.apply.config import TokenFactorySettings
    from conduit.apply.impact_input import load_impact_sites
    from conduit.apply.testrun import judge, record_baseline, run_tests
    from conduit.apply.verify import remaining_breaks

    if not os.environ.get("NEBIUS_API_KEY"):
        pytest.skip("NEBIUS_API_KEY is not set")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "council"
    source = os.environ.get("CONDUIT_COUNCIL_REPO", _COUNCIL_REPO)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--depth", "1", source, _COUNCIL_SHA], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "FETCH_HEAD"], check=True)
    python = _council_python(tmp_path, repo)
    test_cmd = f"{shlex.quote(python)} -m pytest -q tests/unit"

    sites = load_impact_sites(_COUNCIL_IMPACT)
    pristine = {site.path: (repo / site.path).read_text(encoding="utf-8") for site in sites}
    baseline = record_baseline(test_cmd, repo)
    assert baseline.usable, "council's tests did not run on the base commit"

    settings = TokenFactorySettings.from_env(enable_thinking=False)
    report = apply_sites(
        sites,
        ApplyConfig(
            root=repo,
            settings=settings,
            write=True,
            test_cmd=test_cmd,
            recheck_impact=True,
            allow_network=True,
            baseline=baseline,
        ),
    )
    for result in report.results:
        print(f"{result.site.path}:{result.site.line} {result.status.value} attempts={result.attempts} dropped={result.dropped}")
    print(f"tokens={report.tokens_used} new_failures={report.new_failures}")
    assert len(report.results) == len(sites)
    assert report.new_failures == [], report.new_failures

    # The tree as written (verified fixes only): nothing fails that passed on the base.
    after = run_tests(test_cmd, repo)
    verdict = judge(after, baseline)
    assert verdict.new == [] and verdict.missing == [], (verdict.new, verdict.missing[:10])

    for result in report.results:
        site, label = result.site, f"{result.site.path}:{result.site.line} {result.status.value}"
        assert result.status in {SiteStatus.FIXED_VERIFIED, SiteStatus.NEEDS_HUMAN, SiteStatus.SKIPPED}, label
        assert result.why.strip(), label
        if site.via_wrapper:
            # The wrapper caller (anthropic_llm.py:34) is fixed at the SDK line, never rewritten.
            assert result.status == SiteStatus.SKIPPED, label
            assert result.diff == "", label
            continue
        if result.status == SiteStatus.SKIPPED:
            # e.g. another site's rewrite already covered this line: it must say so.
            continue
        if result.status == SiteStatus.NEEDS_HUMAN:
            assert result.error.strip(), label
            continue
        # Fixed (verified): written, parses, and Conduit's checks still pass on the final file.
        assert result.diff.strip(), label
        final = (repo / site.path).read_text(encoding="utf-8")
        ast.parse(final)
        assert final != pristine[site.path], label
        span = None
        around = _enclosing_span(pristine[site.path], site.line)
        if around is not None:
            span = _function_span(final, around[0])
            assert span is not None, label
        left = remaining_breaks(repo, site, final, span=span, allow_network=True, original=pristine[site.path])
        assert left == [], (label, left)
        # An argument the original call passed and the rewrite no longer does is reported.
        if around is not None:
            before_kw = _call_keywords(pristine[site.path], around[1], "create")
            after_kw = _call_keywords(final, span, "create")
            gone = before_kw - after_kw
            settings_gone = gone & {"temperature", "top_k", "top_p"}
            assert settings_gone <= set(result.dropped), (label, gone, result.dropped)
            for name in result.dropped:
                assert f"`{name}`" in result.dropped_note and result.dropped_note in result.why, (label, name)

    wrapper = [r for r in report.results if r.site.via_wrapper]
    assert [(r.site.path.rsplit("/", 1)[-1], r.site.line) for r in wrapper] == [("anthropic_llm.py", 34)]


def test_callee_reads_follow_the_value_past_a_stale_annotation(tmp_path: Path):
    """A helper parameter annotated with a type nothing defines any more (its import went
    with the old release) says nothing about the value: the caller's release type decides."""
    from conduit.apply.typecheck import attribute_breaks

    modules = _typed_release(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    caller = "def call(client):\n    reply = client.send(items=[])\n    return from_old(reply)\n"
    chains = [_chain("caller.py", 2, "client.send", "example_sdk.Client.send")]
    (root / "caller.py").write_text(caller, encoding="utf-8")
    helpers = {
        # import removed: the name is bound nowhere
        "def from_old(reply: Completion):\n    return reply.completion\n": True,
        # imported from the release, which no longer defines it
        "from example_sdk.client import Completion\n\ndef from_old(reply: Completion):\n    return reply.completion\n": True,
        # a real local type: trusted, callers are not consulted
        "class Completion:\n    completion = ''\n\ndef from_old(reply: Completion):\n    return reply.completion\n": False,
    }
    for helper, flagged in helpers.items():
        (root / "helper.py").write_text(helper, encoding="utf-8")
        found = attribute_breaks(modules, root, "caller.py", caller, (1, 3), chains, "example-sdk", "2.0")
        hit = any("from_old() gets reply = Reply" in text and "no attribute completion" in text for text in found)
        assert hit is flagged, (helper, found)


def test_final_recheck_drops_a_rewrite_whose_dependency_was_reverted(tmp_path: Path, monkeypatch):
    """The caller passed its recheck while the helper's rewrite was in place; once the helper
    is reverted the caller is rechecked again, and it names the site it depends on."""
    import conduit.apply.engine as engine

    def fake(root, site, patched, *, span=None, fixtures=None, allow_network=False, original=None):
        if original is None:
            return []
        if site.path == "helper.py":
            return [("helper.py:2 result.content: masked read", "")]
        if "result.completion" in (Path(root) / "helper.py").read_text(encoding="utf-8"):
            return [("helper.py:2 result.completion: from_completion() gets result = Reply from caller.py:2, which has no attribute completion", "")]
        return []

    monkeypatch.setattr(engine, "remaining_breaks", fake)
    root = tmp_path / "app"
    root.mkdir()
    (root / "helper.py").write_text("def from_completion(result):\n    return result.completion\n", encoding="utf-8")
    (root / "caller.py").write_text(
        "def post(client):\n    result = client.old_call(1)\n    return from_completion(result)\n", encoding="utf-8"
    )
    payload = _impact_payload(path="caller.py", line=2)
    payload["impacts"][0]["sites"][0]["expr"] = "client.old_call"
    payload["impacts"][0]["sites"].append(
        {"path": "helper.py", "line": 2, "expr": "result.completion", "confidence": "high", "via_wrapper": False}
    )
    client = FakeModelClient(
        responses=[
            _fn("def post(client):\n    result = client.new_call(1)\n    return from_completion(result)\n"), "",
            _fn("def from_completion(result):\n    return result.content\n"), "read content",
        ]
    )
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    caller, helper = report.results
    assert helper.status == SiteStatus.NEEDS_HUMAN, (helper.status, helper.error)
    assert caller.status == SiteStatus.NEEDS_HUMAN, (caller.status, caller.why)
    assert "no attribute completion" in caller.why and "depends on helper.py:2" in caller.why, caller.why
    assert "result.completion" in (root / "helper.py").read_text(encoding="utf-8")


def test_fixed_result_never_has_an_empty_why(tmp_path: Path):
    """The model's one-line summary can come back empty: the reason is read from the change."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "caller.py").write_text("def post(client):\n    return client.old_call(1)\n", encoding="utf-8")
    payload = _impact_payload(path="caller.py", line=2)
    payload["impacts"][0]["sites"][0]["expr"] = "client.old_call"
    client = FakeModelClient(responses=[_fn("def post(client):\n    return client.new_call(1)\n"), ""])
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"", client=client),
    )
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error)
    assert result.why.startswith("fixed `client.old_call` (parameter old_call renamed to new_call on Client)"), result.why
    assert "now reads `client.new_call`" in result.why, result.why


def test_breaks_message_lists_each_finding_and_field_list_once():
    from conduit.apply.engine import _breaks_message

    fields = "(fields: id: str, content: List[Block], role: Literal['assistant'])"
    first = f"f.py:3 hasattr(r, 'completion'): r is Msg, which has no attribute completion {fields}; drop that branch"
    second = f"f.py:4 r.completion: r is Msg, which has no attribute completion {fields}"
    message = _breaks_message([first, second, first])
    assert message.count("f.py:3") == 1 and message.count("f.py:4") == 1, message
    assert message.count("(fields: ") == 1 and "(fields as above)" in message, message


def test_callee_reads_record_the_value_flow_for_the_consumer(tmp_path: Path):
    """The tracing that flags a read in a helper also records what the helper now receives."""
    from conduit.apply.typecheck import attribute_breaks

    modules = _typed_release(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    caller = "def call(client):\n    reply = client.send(items=[])\n    return from_old(reply)\n"
    (root / "caller.py").write_text(caller, encoding="utf-8")
    (root / "helper.py").write_text("def from_old(reply):\n    return reply.text\n", encoding="utf-8")
    chains = [_chain("caller.py", 2, "client.send", "example_sdk.Client.send")]
    flows: list[dict] = []
    found = attribute_breaks(modules, root, "caller.py", caller, (1, 3), chains, "example-sdk", "2.0", flows)
    assert found == [], found
    assert len(flows) == 1, flows
    flow = flows[0]
    assert (flow["callee_path"], flow["callee"], flow["param"], flow["types"]) == ("helper.py", "from_old", "reply", "Reply")
    assert flow["from"] == "caller.py:3" and flow["release"] == "example-sdk 2.0"
    assert any(field.startswith("text: ") for field in flow["fields"]), flow


def test_batch_rewrites_the_producer_first_and_tells_the_consumer_what_it_gets(tmp_path: Path, monkeypatch):
    """The helper site is listed first, but the caller feeds it: the caller is rewritten first,
    and the helper's prompt carries the caller's rewrite and the traced new-release type."""
    import conduit.apply.engine as engine
    from conduit.apply.typecheck import FLOWS

    def fake(root, site, patched, *, span=None, fixtures=None, allow_network=False, original=None):
        sink = FLOWS.get()
        if sink is not None and site.path == "caller.py" and "new_call" in patched:
            sink.append({
                "callee_path": "helper.py", "callee": "from_completion", "start": 1, "end": 2,
                "param": "result", "types": "Reply", "fields": ["content: List[Block]"],
                "from": "caller.py:3", "release": "example-sdk 2.0",
            })
        return []

    monkeypatch.setattr(engine, "remaining_breaks", fake)
    root, sites = _masked_read_repo(tmp_path)
    assert sites[0].path == "helper.py"
    client = FakeModelClient(responses=[_fn(_CALLER_EDIT), "switched", _fn(_READ_TEXT), "reads content"])
    report = apply_sites(
        sites,
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    first, second = (call["turns"][-1].content for call in client.calls[0:3:2])
    assert '"file": "caller.py"' in first and "connected_rewrites" not in first
    assert '"file": "helper.py"' in second and "connected_rewrites" in second
    assert "result = Reply (example-sdk 2.0)" in second and "content: List[Block]" in second
    assert "client.new_call(1)" in second  # the producer's rewritten code
    assert "connected_rewrites" in client.calls[2]["turns"][0].content
    helper, caller = report.results  # report keeps the usual order
    assert helper.site.path == "helper.py" and helper.status == SiteStatus.FIXED_VERIFIED
    assert caller.status == SiteStatus.FIXED_VERIFIED


def test_final_recheck_finding_gets_one_retry_before_needs_human(tmp_path: Path, monkeypatch):
    """A site the final recheck flags is sent the finding once; a clean retry is verified."""
    import conduit.apply.engine as engine

    def fake(root, site, patched, *, span=None, fixtures=None, allow_network=False, original=None):
        if original is not None and "result.completion" in patched:
            return [("helper.py:2 result.completion: result is Reply in example-sdk 2.0, which has no attribute completion", "")]
        return []

    monkeypatch.setattr(engine, "remaining_breaks", fake)
    root = tmp_path / "app"
    root.mkdir()
    (root / "helper.py").write_text("def from_completion(result):\n    return result.completion\n", encoding="utf-8")
    payload = _impact_payload(path="helper.py", line=2)
    payload["impacts"][0]["sites"][0]["expr"] = "result.completion"
    client = FakeModelClient(
        responses=[
            _fn("def from_completion(result: Any):\n    return result.completion\n"), "widened",
            _fn("def from_completion(result):\n    return result.content\n"), "reads content",
        ]
    )
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    (helper,) = report.results
    assert helper.status == SiteStatus.FIXED_VERIFIED, (helper.status, helper.error)
    assert "result.content" in (root / "helper.py").read_text(encoding="utf-8")
    retry = client.calls[2]["turns"][-1].content
    assert "no attribute completion" in retry and "return result.completion" in retry
    assert len(client.calls) == 4 and report.tokens_used > 0
    assert all(call["enable_thinking"] is False for call in client.calls)


def test_a_type_field_list_is_printed_once_across_the_report():
    from conduit.apply.engine import ApplyReport, _fill_why
    from conduit.apply.models import ImpactSite, SiteResult

    def site(path: str, line: int) -> ImpactSite:
        return ImpactSite(path=path, line=line, expr="x", dependency="sdk", from_version="1", to_version="2",
                          change="gone", confidence="high", source="wheel")

    fields = "(BetaMessage fields: id: str, content: List[Block])"
    first = f"rewrite still breaks: pkg/anthropic.py:61 result.completion: no attribute completion {fields}"
    second = f"rewrite still breaks: pkg/anthropic.py:61 from_completion() gets result, no attribute completion {fields}; read it here"
    report = ApplyReport(results=[
        SiteResult(site=site("pkg/anthropic.py", 60), status=SiteStatus.NEEDS_HUMAN, error=first),
        SiteResult(site=site("pkg/caller.py", 30), status=SiteStatus.NEEDS_HUMAN, why=second, error=second),
    ])
    _fill_why(report)
    one, two = report.results
    # The full findings (model-facing, in ``error``) list the fields once ...
    assert one.error.count("(BetaMessage fields: ") == 1, one.error
    assert "(BetaMessage fields: " not in two.error
    assert "(BetaMessage fields listed at anthropic.py:60); read it here" in two.error, two.error
    # ... and the human ``why`` lists none.
    assert "fields" not in one.why and "fields" not in two.why, (one.why, two.why)


def _union_modules(tmp_path: Path):
    from conduit.apply.signatures import _modules

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(_UNION_WHEEL, encoding="utf-8")
    return _modules(str(tmp_path / "wheel"))


def _guard_findings(tmp_path: Path, body: str) -> list[str]:
    from conduit.apply.typecheck import attribute_breaks

    modules = _union_modules(tmp_path)
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    source = "def post(client):\n    result = client.notes.create(model='m')\n" + textwrap.indent(textwrap.dedent(body), "    ")
    (root / "f.py").write_text(source, encoding="utf-8")
    chains = [_chain("f.py", 2, "client.notes.create", "example_sdk.client.Client.notes.create")]
    return attribute_breaks(modules, root, "f.py", source, (1, len(source.splitlines())), chains, "example-sdk", "2.0")


@pytest.mark.parametrize(
    "body",
    [
        "return ''.join(block.text for block in result.content if hasattr(block, 'text'))\n",
        """\
        parts = []
        for block in result.content:
            if hasattr(block, 'text'):
                parts.append(block.text)
        return ''.join(parts)
        """,
        """\
        parts = []
        for block in result.content:
            if not hasattr(block, 'text'):
                continue
            parts.append(block.text)
        return ''.join(parts)
        """,
    ],
)
def test_filter_only_guard_is_accepted(tmp_path: Path, body: str):
    """A guard that only skips items lacking the attribute makes nothing up for them."""
    found = _guard_findings(tmp_path, body)
    assert not [text for text in found if "'text'" in text or ".text" in text], found


@pytest.mark.parametrize(
    "body",
    [
        "return ''.join(getattr(block, 'text', str(block)) for block in result.content)\n",
        "return ''.join(block.text if hasattr(block, 'text') else str(block) for block in result.content)\n",
        """\
        parts = []
        for block in result.content:
            if hasattr(block, 'text'):
                parts.append(block.text)
            else:
                parts.append('')
        return ''.join(parts)
        """,
    ],
)
def test_fallback_default_guard_is_rejected(tmp_path: Path, body: str):
    """A guard that puts a stand-in value where the attribute is missing is still a finding."""
    found = _guard_findings(tmp_path, body)
    assert any("'text'" in text and "ToolBlock" in text for text in found), found


def test_filter_guard_on_an_attribute_no_item_type_has_is_rejected(tmp_path: Path):
    """Filtering on an attribute none of the release's item types declare drops every item."""
    found = _guard_findings(
        tmp_path, "return ''.join(block.body for block in result.content if hasattr(block, 'body'))\n"
    )
    assert any("hasattr(block, 'body')" in text and "no attribute body" in text for text in found), found


_CHAIN_WHEEL = """\
from typing import Optional

class Usage:
    input_tokens: int

class Reply:
    usage: Optional[Usage]

class Notes:
    def create(self, *, model: str) -> Reply:
        return Reply()

class Client:
    @property
    def notes(self):
        return Notes(self)
"""


def test_getattr_default_on_an_attribute_chain_is_checked(tmp_path: Path):
    """``getattr(result.usage, "x", 0)``: the chain's type comes from the release's field annotations."""
    from conduit.apply.signatures import _modules
    from conduit.apply.typecheck import attribute_breaks

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(_CHAIN_WHEEL, encoding="utf-8")
    modules = _modules(str(tmp_path / "wheel"))
    root = tmp_path / "repo"
    root.mkdir()
    source = textwrap.dedent(
        """\
        def post(client):
            result = client.notes.create(model='m')
            made_up = getattr(result.usage, 'cache_tokens', 0)
            dead = getattr(result.usage, 'input_tokens', 0)
            return made_up, dead, result.usage.input_tokens
        """
    )
    (root / "f.py").write_text(source, encoding="utf-8")
    chains = [_chain("f.py", 2, "client.notes.create", "example_sdk.client.Client.notes.create")]
    found = attribute_breaks(modules, root, "f.py", source, (1, 5), chains, "example-sdk", "2.0")
    assert any("getattr(result.usage, 'cache_tokens', 0)" in t and "result.usage is Usage" in t and "no attribute cache_tokens" in t for t in found), found
    assert any("getattr(result.usage, 'input_tokens', 0)" in t and "always has input_tokens" in t for t in found), found
    assert len(found) == 2, found


def test_calls_into_the_repos_own_code_are_checked_against_its_signature(tmp_path: Path):
    from conduit.apply.typecheck import repo_call_breaks

    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "usage.py").write_text(
        "class Usage:\n    def __init__(self, prompt_tokens: int, completion_tokens: int, cached: int = 0):\n        pass\n",
        encoding="utf-8",
    )
    calls = {
        "Usage(input_tokens=1, completion_tokens=2)": "takes no keyword input_tokens",
        "Usage(prompt_tokens=1)": "requires completion_tokens",
        "Usage(1, 2, 3, 4)": "takes 3 positional arguments",
        "Usage(1, completion_tokens=2, cached=3)": "",
    }
    for call, expected in calls.items():
        source = f"from pkg.usage import Usage\n\ndef build():\n    return {call}\n"
        found = repo_call_breaks(root, "pkg/build.py", source, (3, 4))
        if expected:
            assert len(found) == 1 and expected in found[0] and "Usage in this repo" in found[0], (call, found)
        else:
            assert found == [], (call, found)


def test_site_fed_by_a_caller_that_needs_a_human_is_not_verified(tmp_path: Path, monkeypatch):
    """The helper's rewrite expects the caller's new value; the caller was never fixed."""
    import conduit.apply.engine as engine

    def fake(root, site, patched, *, span=None, fixtures=None, allow_network=False, original=None):
        if site.path == "caller.py" and "new_call" in patched:
            return [("caller.py:2 client.new_call(1): new_call takes no positional arguments", "")]
        return []

    monkeypatch.setattr(engine, "remaining_breaks", fake)
    root, sites = _masked_read_repo(tmp_path)
    client = FakeModelClient(responses=[_fn(_CALLER_EDIT)] * 3 + [_fn(_READ_TEXT), "reads content"])
    report = apply_sites(
        sites,
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    helper, caller = report.results
    assert caller.status == SiteStatus.NEEDS_HUMAN
    assert helper.status == SiteStatus.NEEDS_HUMAN, (helper.status, helper.why)
    # The caller is its producer and was fixed first: the helper is not rewritten against it.
    assert helper.why == "Not rewritten: its caller caller.py:2 was not fixed.", helper.why
    assert helper.error.startswith("its caller caller.py:2 was not fixed"), helper.error
    assert helper.diff == ""
    assert "result.completion" in (root / "helper.py").read_text(encoding="utf-8")  # untouched


def test_rewrite_that_repeats_a_decorator_is_rejected():
    from conduit.apply.engine import _repeated_decorator

    doubled = "class A:\n    @staticmethod\n    @staticmethod\n    def f(x):\n        return x\n"
    assert _repeated_decorator(doubled, (2, 5)) == "@staticmethod on f"
    assert _repeated_decorator(doubled.replace("    @staticmethod\n", "", 1), (2, 4)) == ""


def test_repo_call_check_uses_the_definition_the_module_sees(tmp_path: Path):
    """Two classes share a name: the caller's own module (or its import) decides which one."""
    from conduit.apply.typecheck import repo_call_breaks

    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "other.py").write_text("class Usage:\n    def __init__(self, input_tokens):\n        pass\n", encoding="utf-8")
    (root / "pkg" / "usage.py").write_text("class Usage:\n    def __init__(self, prompt_tokens):\n        pass\n", encoding="utf-8")
    same = "class Usage:\n    def __init__(self, prompt_tokens):\n        pass\n\ndef build():\n    return Usage(input_tokens=1)\n"
    assert any("takes no keyword input_tokens" in t for t in repo_call_breaks(root, "pkg/mine.py", same, (5, 6)))
    imported = "from .usage import Usage\n\ndef build():\n    return Usage(input_tokens=1)\n"
    assert any("takes no keyword input_tokens" in t for t in repo_call_breaks(root, "pkg/mine.py", imported, (3, 4)))
    assert repo_call_breaks(root, "pkg/mine.py", imported.replace("input_tokens", "prompt_tokens"), (3, 4)) == []


def _import_site_repo(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        "from __future__ import annotations\nfrom sdk import Old\n\n\ndef use(x: Old):\n    return x\n", encoding="utf-8"
    )
    payload = _impact_payload(path="mod.py", line=2)
    payload["impacts"][0]["sites"][0]["expr"] = "import Old"
    return root, sites_from_payload(payload)


@pytest.mark.parametrize("retry, verified", [("", False), ("Old = object", True)])
def test_a_removed_import_still_used_gets_one_retry_then_needs_a_human(tmp_path: Path, monkeypatch, retry, verified):
    """The rewrite drops an import an annotation still names (never evaluated under
    ``from __future__ import annotations``): one retry with the hint, else needs-human."""
    import conduit.apply.engine as engine

    monkeypatch.setattr(engine, "remaining_breaks", lambda *a, **k: [])
    root, sites = _import_site_repo(tmp_path)
    client = FakeModelClient(responses=[_fn(""), "removed the import", _fn(retry), "retried"])
    report = apply_sites(
        sites,
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    (result,) = report.results
    hint = client.calls[2]["turns"][-1].content
    assert "name Old is no longer imported or defined" in hint and "new release" in hint, hint
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "from sdk import Old" not in text  # the removal stays in the diff either way
    if verified:
        assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error)
    else:
        assert result.status == SiteStatus.NEEDS_HUMAN, (result.status, result.why)
        assert result.why == "Line 4 still uses `Old`, which this rewrite stopped importing or defining.", result.why
        assert "use the type the call now returns" in result.error


def test_undefined_names_cover_string_annotations_and_ignore_names_still_bound():
    import ast as _ast

    from conduit.apply.engine import _bound_names, _used_names

    before = _ast.parse("from x import A, B\n\ndef f(a: 'A', b: B):\n    return a\n")
    after = _ast.parse("from x import B\n\ndef f(a: 'A', b: B):\n    return a\n")
    lost = _bound_names(before) - _bound_names(after)
    uses = _used_names(after)
    assert lost == {"A"} and uses["A"] == 3 and "B" in uses


def test_plain_why_is_one_short_sentence_without_field_lists_or_instructions():
    from conduit.apply.plain import plain_why

    fields = "(BetaMessage fields: id: str, content: List[BetaContentBlock])"
    raw = (
        "rewrite still breaks against the new release: pkg/a.py:61 result.completion: from_completion() gets "
        f"result = BetaMessage from pkg/b.py:36 in anthropic 1.0.0, which has no attribute completion {fields}; "
        "read what you need here instead of passing it on; depends on pkg/a.py:60, whose fix was not applied (needs a human)"
    )
    assert plain_why(raw, "pkg/b.py") == (
        "`from_completion()` reads `.completion`, which the `BetaMessage` this line now passes doesn't have "
        "in 1.0.0; depends on a.py:60 (not fixed)."
    )
    assert plain_why("rewrite repeats a decorator the function already has (@staticmethod on f); give "
                     "function_source without the existing decorators", "x.py") == "The rewrite added a second `@staticmethod`."
    same = "rewrite still breaks against the new release: pkg/a.py:61 result.completion: result is BetaMessage in anthropic 1.0.0, which has no attribute completion " + fields
    assert plain_why(same, "pkg/a.py") == "Line 61 reads `.completion`, which `BetaMessage` doesn't have in 1.0.0."
    assert plain_why("reverted: the tests still fail with this rewrite\nTraceback ...", "x.py") == "Reverted: the tests failed with this rewrite."
    odd = "token budget exhausted for this site"
    assert plain_why(odd, "x.py") == "token budget exhausted for this site."


def _removed_import_repo(tmp_path: Path, *, caller: bool = False):
    """``from sdk import Old`` is removed; ``use`` still has it as an annotation."""
    root = tmp_path / "app"
    root.mkdir()
    body = "from __future__ import annotations\nfrom sdk import Old\n\n\ndef use(x: Old):\n    return x\n"
    if caller:
        body += "\n\ndef run(client):\n    return use(client.make())\n"
    (root / "mod.py").write_text(body, encoding="utf-8")
    payload = _impact_payload(path="mod.py", line=2)
    payload["dependency"] = "sdk"
    impact = payload["impacts"][0]
    impact.update(dependency="sdk", kind="removed", symbol="sdk.types.old.Old", change="old.Old removed")
    impact["sites"][0]["expr"] = "import Old"
    return root, sites_from_payload(payload)


def test_removed_name_context_lists_every_use_and_the_type_that_now_arrives(tmp_path: Path):
    """Fix 1: an import site sees the annotation that still names the removed type,
    and the new-release type an accepted rewrite now passes there (with its import)."""
    from conduit.apply.context import build_context_pack
    from conduit.apply.engine import _RouteState, _with_received_types
    from conduit.apply.prompts import propose_messages

    root, (site,) = _removed_import_repo(tmp_path)
    pack = build_context_pack(root, site)
    assert pack.removed_name_uses == [
        {"name": "Old", "line": 5, "code": "def use(x: Old):", "in_this_function": False, "function": "use", "param": "x"}
    ]
    flow = {"callee_path": "mod.py", "callee": "use", "param": "x", "types": "New", "fields": ["text: str"],
            "from": "mod.py:10", "release": "sdk 2.0", "imports": ["from sdk.types import New"]}
    routes = _RouteState(accepted={1: {"site": site, "flows": [flow]}}, key=2)
    config = ApplyConfig(root=root, settings=_settings(), routes=routes)
    (use,) = _with_received_types(config, site, pack.removed_name_uses)
    assert use["now_receives"] == "New (sdk 2.0), passed from mod.py:10"
    assert use["import_for_it"] == ["from sdk.types import New"]
    pack.removed_name_uses = [use]
    system, user = (text for _role, text in propose_messages(pack))
    assert "removed_name_uses" in system and "line_edits" in system
    assert '"now_receives": "New (sdk 2.0), passed from mod.py:10"' in user


def test_line_edits_change_only_listed_lines_and_survive_a_rebuild(tmp_path: Path):
    """Fix 1: the import site may also rewrite the annotation line it lists, one line for one."""
    from conduit.apply.context import build_context_pack
    from conduit.apply.splice import PatchError, parse_model_edit, resolve_line_edits, splice_edit_with_span

    root, (site,) = _removed_import_repo(tmp_path)
    pack = build_context_pack(root, site)
    reply = json.dumps({"function_source": "from sdk.types import New", "new_imports": [],
                        "line_edits": [{"line": 5, "new": "def use(x: New):"}]})
    edit = resolve_line_edits(pack, parse_model_edit(reply))
    assert edit.line_edits == ((5, "def use(x: Old):", "def use(x: New):"),)
    patched, _diff, _span = splice_edit_with_span(pack, edit)
    assert "from sdk.types import New\n" in patched and "def use(x: New):" in patched and "Old" not in patched
    # A rebuild finds the line by its text, even after other lines moved.
    shifted = pack.full_source.replace("\n\n\ndef use", "\n\n\n\n\ndef use")
    (root / "mod.py").write_text(shifted, encoding="utf-8")
    again, _diff, _span = splice_edit_with_span(build_context_pack(root, site), edit)
    assert "def use(x: New):" in again and "Old" not in again
    bad = json.dumps({"function_source": "", "new_imports": [], "line_edits": [{"line": 1, "new": "x = 1"}]})
    with pytest.raises(PatchError, match="only change lines listed"):
        resolve_line_edits(pack, parse_model_edit(bad))


def test_import_site_rewritten_with_its_use_is_verified(tmp_path: Path, monkeypatch):
    """Fix 1 end to end: the import and the annotation change together; nothing is left undefined."""
    import conduit.apply.engine as engine

    monkeypatch.setattr(engine, "remaining_breaks", lambda *a, **k: [])
    root, sites = _removed_import_repo(tmp_path)
    reply = json.dumps({"function_source": "from sdk.types import New", "new_imports": [],
                        "line_edits": [{"line": 5, "new": "def use(x: New):"}]})
    client = FakeModelClient(responses=[reply, "imports the new type"])
    report = apply_sites(
        sites,
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error)
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "from sdk.types import New" in text and "def use(x: New):" in text and "Old" not in text
    assert "removed_name_uses" in client.calls[0]["turns"][-1].content


def _graph_repo(tmp_path: Path):
    """``run`` calls ``use``; ``use`` is annotated with the removed ``Old``, imported on line 2."""
    root, (import_site,) = _removed_import_repo(tmp_path, caller=True)
    payload = _impact_payload(path="mod.py", line=5)
    payload["dependency"] = "sdk"
    impact = payload["impacts"][0]
    impact.update(dependency="sdk", kind="removed", symbol="sdk.types.old.Old", change="old.Old removed")
    impact["sites"] = [
        {"path": "mod.py", "line": 5, "expr": "Old"},
        {"path": "mod.py", "line": 10, "expr": "client.make"},
    ]
    use_site, run_site = sites_from_payload(payload)
    return root, import_site, use_site, run_site


def test_dependency_order_puts_producers_first_and_groups_cycles(tmp_path: Path):
    """Fix 2: callers first, then the import whose replacement type they decide, then its use."""
    from conduit.apply.engine import _RouteState, _dependency_order

    root, import_site, use_site, run_site = _graph_repo(tmp_path)
    routes = _RouteState(accepted={})
    order = _dependency_order(root, [use_site, import_site, run_site], {}, routes)
    assert [site.line for site in order] == [10, 2, 5]
    assert sorted(routes.feeders[id(use_site)]) == sorted([id(import_site), id(run_site)])
    assert routes.edge_kind[(id(use_site), id(import_site))] == "import"
    assert routes.edge_kind[(id(use_site), id(run_site))] == "call"
    assert id(import_site) not in routes.feeders  # the caller only orders the import, it does not block it
    # Two functions that call each other are one group: given order, no blocking between them.
    (root / "loop.py").write_text("def a(n):\n    return b(n)\n\n\ndef b(n):\n    return a(n)\n", encoding="utf-8")
    payload = _impact_payload(path="loop.py", line=2)
    payload["impacts"][0]["sites"] = [{"path": "loop.py", "line": 2, "expr": "b"}, {"path": "loop.py", "line": 6, "expr": "a"}]
    first, second = sites_from_payload(payload)
    routes = _RouteState(accepted={})
    assert [site.line for site in _dependency_order(root, [first, second], {}, routes)] == [2, 6]
    assert routes.feeders == {}


def test_a_site_whose_import_needs_a_human_is_not_rewritten(tmp_path: Path, monkeypatch):
    """Fix 2: the import site fails, so its use is flagged naming it, not rewritten on a broken base."""
    import conduit.apply.engine as engine

    monkeypatch.setattr(engine, "remaining_breaks", lambda *a, **k: [])
    root, import_site, use_site, _run = _graph_repo(tmp_path)
    client = FakeModelClient(responses=["not json"] * 3)
    report = apply_sites(
        [import_site, use_site],
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    first, second = report.results
    assert first.site.line == 2 and first.status == SiteStatus.NEEDS_HUMAN
    assert second.status == SiteStatus.NEEDS_HUMAN and second.diff == ""
    assert second.why == "Not rewritten: the import or definition at line 2 it uses was not fixed.", second.why
    assert len(client.calls) == 3  # only the import site's attempts


def test_decorators_are_kept_once_when_the_model_repeats_them(tmp_path: Path):
    """Fix 3: the def goes out without its decorators; copies the model sends back are dropped."""
    from conduit.apply.context import build_context_pack
    from conduit.apply.prompts import propose_messages
    from conduit.apply.splice import ModelEdit, splice_edit_with_span, strip_kept_decorators

    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        "import functools\n\n\nclass A:\n    @staticmethod\n    @functools.cache\n    def f(x):\n        return x.old\n",
        encoding="utf-8",
    )
    payload = _impact_payload(path="mod.py", line=8)
    (site,) = sites_from_payload(payload)
    pack = build_context_pack(root, site)
    assert pack.function_source.lstrip().startswith("def f(x):")
    assert pack.decorators == ["staticmethod", "functools.cache"]
    system, user = (text for _role, text in propose_messages(pack))
    assert "decorators_kept" in system and '"@staticmethod"' in user
    repeated = "@staticmethod\n@functools.cache\n@other\ndef f(x):\n    return x.new\n"
    assert strip_kept_decorators(repeated, pack.decorators) == "@other\ndef f(x):\n    return x.new\n"
    patched, _diff, span = splice_edit_with_span(pack, ModelEdit(repeated.replace("@other\n", ""), ()))
    assert patched.count("@staticmethod") == 1 and patched.count("@functools.cache") == 1
    assert "return x.new" in patched
    from conduit.apply.engine import _repeated_decorator

    assert _repeated_decorator(patched, (span[0] - 2, span[1])) == ""  # the backstop stays quiet


_OPTIONAL_RELEASE = textwrap.dedent(
    """\
    from typing import Optional, Union

    class BaseModel:
        pass

    class Usage(BaseModel):
        input_tokens: int
        cache_read_input_tokens: Optional[int] = None
        cache_creation_input_tokens: int | None = None
        server: Union[str, None]
        tier: str = None
    """
)


def _none_findings(tmp_path: Path, body: str, repo_usage: str = "") -> list[str]:
    import ast as _ast

    from conduit.apply.nonecheck import none_breaks

    modules = {"sdk.types": _ast.parse(_OPTIONAL_RELEASE)}
    klass = next(node for node in modules["sdk.types"].body if isinstance(node, _ast.ClassDef) and node.name == "Usage")
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    usage = repo_usage or (
        "class Tokens:\n    def __init__(self, prompt: int, cached: int):\n"
        "        self.total = prompt + cached\n"
    )
    (root / "tokens.py").write_text(usage, encoding="utf-8")
    source = "from tokens import Tokens\n\n" + textwrap.dedent(body)
    (root / "mod.py").write_text(source, encoding="utf-8")
    tree = _ast.parse(source)
    func = next(node for node in tree.body if isinstance(node, _ast.FunctionDef))
    typed = {"u": [("sdk.types", klass)]}
    return none_breaks(modules, root, {"mod.py": tree}, "mod.py", tree, func, typed, "sdk", "2.0")


def test_optional_field_passed_where_the_callee_adds_it_up_is_flagged(tmp_path: Path):
    from conduit.apply.plain import plain_why

    found = _none_findings(tmp_path, "def f(u):\n    return Tokens(prompt=u.input_tokens, cached=u.cache_read_input_tokens)\n")
    assert len(found) == 1 and "cache_read_input_tokens may be None (Optional[int]) in sdk 2.0" in found[0], found
    assert "`Tokens` adds it up" in found[0] and "or 0" in found[0]
    why = plain_why("rewrite still breaks against the new release: " + found[0], "mod.py")
    assert why == "`cache_read_input_tokens` can be None in 2.0, but `Tokens` adds it up.", why
    # Positional, an alias, the `X | None` spelling and a None default on a plain annotation all count.
    assert _none_findings(tmp_path, "def f(u):\n    return Tokens(1, u.cache_creation_input_tokens)\n")
    assert _none_findings(tmp_path, "def f(u):\n    t = u.cache_read_input_tokens\n    return Tokens(1, t)\n")
    assert _none_findings(tmp_path, "def f(u):\n    return Tokens(1, u.tier)\n")
    # A non-Optional field is fine.
    assert _none_findings(tmp_path, "def f(u):\n    return Tokens(1, u.input_tokens)\n") == []


@pytest.mark.parametrize(
    "body",
    [
        "def f(u):\n    return Tokens(1, u.cache_read_input_tokens or 0)\n",
        "def f(u):\n    c = u.cache_read_input_tokens\n    return Tokens(1, c if c is not None else 0)\n",
        "def f(u):\n    if u.cache_read_input_tokens is None:\n        return None\n    return Tokens(1, u.cache_read_input_tokens)\n",
        "def f(u):\n    if u.cache_read_input_tokens:\n        return Tokens(1, u.cache_read_input_tokens)\n",
    ],
)
def test_handled_none_is_not_flagged(tmp_path: Path, body: str):
    assert _none_findings(tmp_path, body) == []


def test_callee_that_accepts_none_or_only_stores_it_is_fine(tmp_path: Path):
    body = "def f(u):\n    return Tokens(1, u.cache_read_input_tokens)\n"
    optional = "from typing import Optional\n\nclass Tokens:\n    def __init__(self, prompt: int, cached: Optional[int]):\n        self.total = prompt + (cached or 0)\n"
    assert _none_findings(tmp_path, body, optional) == []
    stores = "class Tokens:\n    def __init__(self, prompt, cached):\n        self.cached = cached\n"
    assert _none_findings(tmp_path, body, stores) == []
    guarded = "class Tokens:\n    def __init__(self, prompt: int, cached=None):\n        self.total = prompt + cached\n"
    assert _none_findings(tmp_path, body, guarded) == []  # its parameter takes None by default
    annotated = "class Tokens:\n    def __init__(self, prompt: int, cached: int):\n        self.cached = cached\n"
    (only,) = _none_findings(tmp_path, body, annotated)
    assert "`Tokens` takes it as `int`" in only


def test_direct_arithmetic_comparison_and_annotated_return_are_flagged(tmp_path: Path):
    add = _none_findings(tmp_path, "def f(u):\n    return u.input_tokens + u.cache_read_input_tokens\n")
    assert len(add) == 1 and "but line 4 adds it up" in add[0], add
    aug = _none_findings(tmp_path, "def f(u):\n    total = 0\n    total += u.cache_read_input_tokens\n    return total\n")
    assert aug and "adds it up" in aug[0]
    cmp = _none_findings(tmp_path, "def f(u):\n    return u.cache_read_input_tokens > 3\n")
    assert cmp and "compares it" in cmp[0]
    assert _none_findings(tmp_path, "def f(u):\n    return u.cache_read_input_tokens is None\n") == []
    ret = _none_findings(tmp_path, "def f(u) -> int:\n    return u.cache_read_input_tokens\n")
    assert ret and "`f` returns it as `int`" in ret[0]
    assert _none_findings(tmp_path, "def f(u) -> 'int | None':\n    return u.cache_read_input_tokens\n") == []


def test_or_zero_on_an_optional_count_is_not_masking():
    """`x or 0` handles None the new type allows; the getattr masking rule is about guessing a field exists."""
    import ast as _ast

    from conduit.apply.typecheck import _masked_read

    modules = {"sdk.types": _ast.parse(_OPTIONAL_RELEASE)}
    klass = next(node for node in modules["sdk.types"].body if isinstance(node, _ast.ClassDef) and node.name == "Usage")
    call = _ast.parse("getattr(u, 'cache_read_input_tokens', 0)", mode="eval").body
    text = _masked_read(modules, "mod.py", call, "u", "cache_read_input_tokens", [("sdk.types", klass)], "sdk", "2.0")
    assert "dead code" in text and "its value may be None" in text and "or ..." in text, text
    plain = _ast.parse("u.cache_read_input_tokens or 0", mode="eval").body
    assert _masked_read(modules, "mod.py", plain, "u", "cache_read_input_tokens", [("sdk.types", klass)], "sdk", "2.0") == ""


@pytest.mark.parametrize("retry, verified", [("unguarded", False), ("guarded", True)])
def test_optional_finding_in_final_recheck_gets_one_retry(tmp_path: Path, monkeypatch, retry, verified):
    """The None check runs on the finished migration: one retry with the finding, else needs-human."""
    import conduit.apply.engine as engine
    from conduit.apply.nonecheck import NONE_CHECK

    seen: list[bool] = []

    def fake(root, site, patched, *, span=None, fixtures=None, allow_network=False, original=None):
        seen.append(NONE_CHECK.get())
        if NONE_CHECK.get() and "new_call(x.cache)" in patched:
            return [(f"{site.path}:2 x.cache: cache may be None (Optional[int]) in example-sdk 2.0, but "
                     "`Tokens` adds it up; handle None where it is read (`x.cache or 0`, ...)", "")]
        return []

    monkeypatch.setattr(engine, "remaining_breaks", fake)
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text("def f(x):\n    return x.old_call(x.cache)\n", encoding="utf-8")
    sites = sites_from_payload(_impact_payload(path="mod.py", line=2))
    first = "def f(x):\n    return x.new_call(x.cache)\n"
    second = first if retry == "unguarded" else "def f(x):\n    return x.new_call(x.cache or 0)\n"
    client = FakeModelClient(responses=[_fn(first), "uses new_call", _fn(second), "guards None"])
    report = apply_sites(
        sites,
        ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                    client=client, recheck_impact=True),
    )
    (result,) = report.results
    assert False in seen and True in seen  # per-site check without it, final recheck with it
    assert "cache may be None" in client.calls[2]["turns"][-1].content
    if verified:
        assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error)
    else:
        assert result.status == SiteStatus.NEEDS_HUMAN
        assert result.why == "`cache` can be None in 2.0, but `Tokens` adds it up.", result.why
        assert "or 0" in result.error


def test_plain_why_for_a_wrong_argument_kind():
    from conduit.apply.plain import plain_why

    raw = (
        "rewrite still breaks against the new release: pkg/llm.py:30 self._client.beta.notes.create: notes= "
        "passes a str but notes.create in example-sdk 1.0.0 expects Iterable[BetaNoteParam], i.e. a list like "
        "[{\"content\": <Union[str, Iterable[BetaContentBlockParam]]>}] (prompt = self.f(notes))"
    )
    assert plain_why(raw, "pkg/llm.py") == (
        "Line 30 passes a `str` as `notes=`, but `notes.create` expects `Iterable[BetaNoteParam]` in 1.0.0."
    )


# --- dropped arguments and import-swap why ---------------------------------


def _old_call(source: str, expr: str = "client.send", line: int = 2):
    from conduit.apply.typecheck import _call_at

    return _call_at(ast.parse(source), line, expr)


def _drops(before: str, after: str, expr: str = "client.send", order: list[str] | None = None):
    from conduit.apply.dropped import dropped_from

    tree = ast.parse(after)
    names, call = dropped_from(_old_call(before, expr), tree, (1, len(after.splitlines())), order)
    return names, call


def test_dropped_keywords_are_found_and_moved_values_are_not():
    before = "def f(client, cfg, p):\n    return client.send(prompt=p, model=cfg.model, limit=cfg.limit, temp=cfg.temp, top=cfg.top)\n"
    after = (
        "def f(client, cfg, p):\n"
        "    msgs = [p]\n"
        "    return client.post(notes=msgs, model=cfg.model, max=cfg.limit)\n"
    )
    names, call = _drops(before, after)
    # limit moved to ``max=``; prompt went into ``msgs`` but ``p`` is still read; temp and top are gone.
    assert names == ["temp", "top"], names
    assert call is not None and call.func.attr == "post"


def test_a_value_read_elsewhere_in_the_function_is_not_dropped_but_a_constant_must_stay_in_the_call():
    before = "def f(client, cfg):\n    return client.send(model='m', temp=cfg.temp, mode='fast')\n"
    after = "def f(client, cfg):\n    extra = {'t': cfg.temp}\n    return client.send(model='m', **extra)\n"
    # ``**extra`` may pass anything: the call is not judged.
    assert _drops(before, after)[0] == []
    after = "def f(client, cfg):\n    t = cfg.temp\n    return client.send(model='m', body=t)\n"
    assert _drops(before, after)[0] == ["mode"]


def test_dropped_positional_arguments_are_named_through_the_old_signature():
    before = "def f(client, a, b):\n    return client.send(a, b)\n"
    after = "def f(client, a, b):\n    return client.send(a)\n"
    assert _drops(before, after, order=["first", "second"])[0] == ["second"]
    assert _drops(before, after)[0] == ["positional argument 2"]
    moved = "def f(client, a, b):\n    return client.send(second=b, first=a)\n"
    assert _drops(before, moved, order=["first", "second"])[0] == []


def test_call_with_nothing_in_common_is_not_compared():
    before = "def f(client, a):\n    return client.send(a, limit=1)\n"
    after = "def f(client, a):\n    return other(3)\n"
    assert _drops(before, after) == ([], None)


def test_dropped_note_says_what_the_new_release_shows():
    from conduit.apply.dropped import note_for

    assert note_for(["temperature", "top_k"], ["temperature", "top_k"], "notes.create") == (
        "Dropped `temperature`, `top_k`: the new release's `notes.create` doesn't accept them, "
        "so these settings are now ignored."
    )
    assert note_for(["x"], [], "") == "Dropped `x`: removed without a matching parameter in the new call."
    assert note_for(["a", "b"], ["a"], "m.go") == (
        "Dropped `a`: the new release's `m.go` doesn't accept it, so this setting is now ignored. "
        "Dropped `b`: removed without a matching parameter in the new call."
    )
    assert note_for([], [], "m.go") == ""


def test_record_drops_reads_the_new_callees_parameters(tmp_path: Path, monkeypatch):
    import conduit.apply.signatures as signatures
    from conduit.apply.dropped import DROPPED
    from conduit.apply.verify import _record_drops
    from types import SimpleNamespace

    pkg = tmp_path / "wheel" / "example_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .client import Client\n", encoding="utf-8")
    (pkg / "client.py").write_text(
        "class Notes:\n    def create(self, *, model: str, max_tokens: int, timeout: float = 1.0):\n        return 1\n\n"
        "class Client:\n    notes: Notes\n",
        encoding="utf-8",
    )
    wheel = tmp_path / "wheel"
    monkeypatch.setattr(signatures, "new_release_wheel", lambda dep, ver, **kw: wheel if ver == "2.0" else None)
    before = (
        "def f(client, cfg):\n"
        "    return client.completions.create(prompt='p', model=cfg.model, max_tokens_to_sample=cfg.n,"
        " temperature=cfg.t, top_k=cfg.k)\n"
    )
    after = (
        "def f(client, cfg):\n"
        "    return client.notes.create(model=cfg.model, max_tokens=cfg.n)\n"
    )
    site = sites_from_payload(_impact_payload(path="mod.py", line=2))[0]
    from dataclasses import replace

    site = replace(site, expr="client.completions.create", symbol="")
    chain = _chain("mod.py", 2, "client.notes.create", "example_sdk.client.Client.notes.create")
    sink: dict = {}
    token = DROPPED.set(sink)
    try:
        _record_drops(site, before, ast.parse(after), (1, 2), SimpleNamespace(chains=[chain]), wheel, None, False)
    finally:
        DROPPED.reset(token)
    assert sink["names"] == ["prompt", "temperature", "top_k"], sink
    assert sink["note"] == (
        "Dropped `prompt`, `temperature`, `top_k`: the new release's `notes.create` doesn't accept them, "
        "so these settings are now ignored."
    ), sink["note"]


def test_dropped_note_is_in_the_why_and_the_json_and_status_stays_verified(tmp_path: Path, monkeypatch):
    import conduit.apply.engine as engine
    from conduit.apply.dropped import DROPPED

    def fake(root, site, patched, *, span=None, fixtures=None, allow_network=False, original=None):
        sink = DROPPED.get()
        if sink is not None and "new_call" in patched:
            sink["names"] = ["old_extra"]
            sink["note"] = "Dropped `old_extra`: removed without a matching parameter in the new call."
        return []

    monkeypatch.setattr(engine, "remaining_breaks", fake)
    # The per-site check only reads the sink in _type_breaks, so call the real verify path through the fake.
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text("def f(x):\n    return x.old_call(1, old_extra=2)\n", encoding="utf-8")
    sites = sites_from_payload(_impact_payload(path="mod.py", line=2))
    client = FakeModelClient(responses=[_fn("def f(x):\n    return x.new_call(1)\n"), "Switched to new_call.\nSecond line."])
    config = ApplyConfig(root=root, settings=_settings(), write=True, test_cmd="python3 -c \"print('ok')\"",
                         client=client, recheck_impact=True)
    report = apply_sites(sites, config)
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_VERIFIED, (result.status, result.error)
    assert result.dropped == ["old_extra"]
    assert result.why == "Switched to new_call. Dropped `old_extra`: removed without a matching parameter in the new call."
    assert "\n" not in result.why
    data = result.to_dict()
    assert data["dropped"] == ["old_extra"] and data["dropped_note"].startswith("Dropped `old_extra`")
    from conduit.apply.report import format_report

    assert "Dropped `old_extra`" in format_report(report)
    engine._fill_why(report)  # idempotent
    assert report.results[0].why.count("Dropped") == 1


def test_a_result_with_no_dropped_arguments_has_an_empty_note(tmp_path: Path, monkeypatch):
    import conduit.apply.engine as engine

    monkeypatch.setattr(engine, "remaining_breaks", lambda *a, **k: [])
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text("def f(x):\n    return x.old_call(1)\n", encoding="utf-8")
    sites = sites_from_payload(_impact_payload(path="mod.py", line=2))
    client = FakeModelClient(responses=[_fn("def f(x):\n    return x.new_call(1)\n"), "Uses new_call."])
    report = apply_sites(sites, ApplyConfig(root=root, settings=_settings(), write=True,
                                            test_cmd="python3 -c \"print('ok')\"", client=client, recheck_impact=True))
    (result,) = report.results
    assert (result.dropped, result.dropped_note, result.why) == ([], "", "Uses new_call.")


def test_import_swap_why_comes_from_the_diff_not_the_model():
    from conduit.apply.dropped import import_swap_why

    before = "import os\nfrom pkg.types import Completion\n\n\ndef f(x: Completion):\n    return x\n"
    after = "import os\nfrom pkg.types.beta import BetaMessage\n\n\ndef f(x: BetaMessage):\n    return x\n"
    assert import_swap_why(before, after, 2, "completion.Completion removed") == (
        "Replaced the import `from pkg.types import Completion` with `from pkg.types.beta import BetaMessage` "
        "(completion.Completion removed)."
    )
    alias = "import os\nfrom pkg.types import Message as Completion\n\n\ndef f(x: Completion):\n    return x\n"
    assert "Message as Completion" in import_swap_why(before, alias, 2)
    assert import_swap_why(before, before, 2) == ""  # unchanged import: nothing to say
    assert import_swap_why(before, after, 5) == ""  # not an import line
    dropped = "import os\n\n\ndef f(x):\n    return x\n"
    assert import_swap_why(before, dropped, 2).startswith("Removed the import `from pkg.types import Completion`")
