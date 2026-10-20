"""Offline unit tests for ``conduit apply`` with a fake model client."""

from __future__ import annotations

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

    def fake_listed(scratch: Path, site, *, fixtures, allow_network) -> bool:
        calls.append(scratch)
        text = (scratch / site.path).read_text(encoding="utf-8")
        return "old_call" in text

    monkeypatch.setattr(verify_mod, "_site_still_listed", fake_listed)
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
    monkeypatch.setattr(verify_mod, "_site_still_listed", lambda *a, **k: True)
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
    assert "still flagged" in report.results[0].error


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
