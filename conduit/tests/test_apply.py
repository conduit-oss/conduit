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
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.patch import PatchError, apply_unified_diff, validate_diff_scope
from conduit.apply.context import build_context_pack
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
                "change": "old_call renamed to new_call",
                "confidence": "high",
                "source": "wheel",
                "kind": "",
                "symbol": "Client.old_call",
                "old": "old_call(self, x)",
                "new": "new_call(self, x)",
                "new_docstring": "Replacement for old_call.",
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


def _good_diff() -> str:
    # mod.py: line 6 = def broken, line 7 = return client.old_call(1)
    return textwrap.dedent(
        """\
        --- a/mod.py
        +++ b/mod.py
        @@ -6,2 +6,2 @@
         def broken(client):
        -    return client.old_call(1)
        +    return client.new_call(1)
        """
    )


def _bad_diff_touches_other() -> str:
    return textwrap.dedent(
        """\
        --- a/mod.py
        +++ b/mod.py
        @@ -3,2 +3,2 @@
         def other():
        -    return helper.keep()
        +    return helper.changed()
        """
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
    assert len(report.results) == 1
    assert report.results[0].status == SiteStatus.DRY_RUN
    assert report.results[0].context is not None
    pack = report.results[0].context
    assert "old_call" in pack.function_source
    assert pack.release_diff["change"] == "old_call renamed to new_call"
    assert (root / "mod.py").read_text(encoding="utf-8").count("old_call") == 1


def test_happy_path_write_verified(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())

    def cleared(_root: Path, site, patched: str) -> bool:
        return "old_call" not in patched and "new_call" in patched

    client = FakeModelClient(
        responses=[
            _good_diff(),
            "Rename old_call to new_call per the release diff.",
        ]
    )
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
    assert "new_call" in (root / "mod.py").read_text(encoding="utf-8")
    assert "old_call" not in (root / "mod.py").read_text(encoding="utf-8")
    assert "other" in (root / "mod.py").read_text(encoding="utf-8")
    assert report.results[0].why


def test_bad_diff_rejected_then_retry_succeeds(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    client = FakeModelClient(
        responses=[
            _bad_diff_touches_other(),
            _good_diff(),
            "Fixed only the flagged function.",
        ]
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


def test_validate_diff_scope_rejects_other_function(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    site = sites_from_payload(_impact_payload())[0]
    pack = build_context_pack(root, site)
    with pytest.raises(PatchError, match="outside"):
        validate_diff_scope(_bad_diff_touches_other(), pack)


def test_retry_on_test_failure_then_needs_human(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    # Three attempts: each produces a valid scoped diff but tests always fail.
    client = FakeModelClient(responses=[_good_diff(), _good_diff(), _good_diff()])
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
    # Working tree unchanged without --write.
    assert "old_call" in (root / "mod.py").read_text(encoding="utf-8")


def test_apply_unified_diff_roundtrip(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    source = (root / "mod.py").read_text(encoding="utf-8")
    patched = apply_unified_diff(source, _good_diff())
    assert "client.new_call(1)" in patched
    assert "client.old_call(1)" not in patched


def test_cli_dry_run(tmp_path: Path):
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
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "dry-run"
    assert payload["context_packs"][0]["function_name"] == "broken"


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
    client = FakeModelClient(responses=[_good_diff(), "why"])
    apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            client=client,
            site_cleared=lambda *_a, **_k: True,
        ),
    )
    assert len(client.calls) == 2
    assert client.calls[0]["enable_thinking"] is False
    assert client.calls[1]["enable_thinking"] is False


def test_enable_thinking_configurable_per_role(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    client = FakeModelClient(responses=[_good_diff(), "why"])
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


def test_token_factory_client_sends_thinking_kwarg_and_ignores_reasoning(monkeypatch):
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
            captured["url"] = url
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
    assert "reasoning" not in result.content
    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_content" not in str(captured["json"])


def test_trim_test_feedback_keeps_last_n_lines():
    from conduit.apply.feedback import TEST_FEEDBACK_MAX_LINES, trim_test_feedback

    lines = [f"line-{i}" for i in range(TEST_FEEDBACK_MAX_LINES + 25)]
    trimmed = trim_test_feedback("\n".join(lines))
    assert "line-0" not in trimmed
    assert f"line-{TEST_FEEDBACK_MAX_LINES + 24}" in trimmed
    assert "earlier lines omitted" in trimmed
    assert trimmed.count("\n") <= TEST_FEEDBACK_MAX_LINES + 1


def test_test_failure_feedback_fed_to_model_is_trimmed(tmp_path: Path):
    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    huge = "\n".join(f"noise-{i}" for i in range(200))
    log_file = tmp_path / "huge.log"
    log_file.write_text(huge + "\n", encoding="utf-8")
    client = FakeModelClient(
        responses=[
            _good_diff(),
            _good_diff(),
            "fixed after retry",
        ]
    )
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
    # Second propose call includes trimmed feedback in the user message.
    second_user = client.calls[1]["messages"][1].content
    assert "noise-0" not in second_user
    assert "earlier lines omitted" in second_user
    assert "noise-199" in second_user


def test_recheck_impact_uses_collect_impacts_on_scratch(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod
    from conduit.impact.model import Impact, SiteHit

    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    calls: list[Path] = []

    def fake_listed(scratch: Path, site, *, fixtures, allow_network) -> bool:
        calls.append(scratch)
        text = (scratch / site.path).read_text(encoding="utf-8")
        # Still flagged only if old_call remains.
        return "old_call" in text

    monkeypatch.setattr(verify_mod, "_site_still_listed", fake_listed)

    client = FakeModelClient(responses=[_good_diff(), "why"])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            recheck_impact=True,
            fixtures=tmp_path / "missing-fixtures-unused",
        ),
    )
    assert report.results[0].status == SiteStatus.FIXED_UNVERIFIED, report.results[0].error
    assert calls, "expected impact re-check against a scratch tree"
    assert "new_call" in (root / "mod.py").read_text(encoding="utf-8")


def test_recheck_impact_rejects_when_site_still_listed(tmp_path: Path, monkeypatch):
    from conduit.apply import verify as verify_mod

    root = _repo_with_break(tmp_path)
    sites = sites_from_payload(_impact_payload())
    monkeypatch.setattr(verify_mod, "_site_still_listed", lambda *a, **k: True)
    client = FakeModelClient(responses=[_good_diff(), _good_diff(), _good_diff()])
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


def test_apply_break_fixture_paths_exist():
    fixture = Path(__file__).resolve().parent / "fixtures" / "apply-break"
    assert (fixture / "db.py").is_file()
    assert (fixture / "impact.json").is_file()
    payload = json.loads((fixture / "impact.json").read_text(encoding="utf-8"))
    sites = sites_from_payload(payload)
    assert sites
    assert sites[0].expr == "query.from_self"


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
            messages=[
                ChatMessage(role="user", content="Reply with exactly: ok"),
            ],
            max_tokens=16,
            enable_thinking=False,
        )
    finally:
        client.close()
    assert result.content
    # Must never appear in assertions via repr of client (api_key is in dataclass).
    assert "NEBIUS_API_KEY" not in result.content
