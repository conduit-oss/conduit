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
        )
    finally:
        client.close()
    assert result.content
    # Must never appear in assertions via repr of client (api_key is in dataclass).
    assert "NEBIUS_API_KEY" not in result.content
