"""Apply transcripts: exact prompt/reply per site, optional one-line plan.

Made-up package only (acmekit). No real SDK, product, or migration-table names.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from conduit.apply.client import FakeModelClient
from conduit.apply.config import TokenFactorySettings
from conduit.apply.engine import (
    TRANSCRIPT_TEXT_MAX,
    ApplyConfig,
    apply_sites,
)
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.prompts import propose_messages


def _settings(**kwargs) -> TokenFactorySettings:
    return TokenFactorySettings.from_env(api_key="test-key-not-for-logging", **kwargs)


def _acmekit_repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        textwrap.dedent(
            """\
            import acmekit

            def run():
                return acmekit.Store("orders")
            """
        ),
        encoding="utf-8",
    )
    return root


def _acmekit_impact(*, path: str = "mod.py", line: int = 4) -> dict:
    return {
        "dependency": "acmekit",
        "impacts": [
            {
                "dependency": "acmekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "Store(name) became Store(api_key, host)",
                "confidence": "high",
                "source": "wheel",
                "kind": "param_renamed",
                "symbol": "acmekit.Store",
                "old": "name",
                "new": "api_key",
                "new_docstring": "Construct a Store with api_key and host.",
                "sites": [
                    {
                        "path": path,
                        "line": line,
                        "expr": "acmekit.Store",
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


def _edit(*, plan: str | None = None, body: str | None = None) -> str:
    source = body or (
        "def run():\n"
        '    return acmekit.Store(api_key="orders", host="local")\n'
    )
    payload: dict = {"function_source": source, "new_imports": []}
    if plan is not None:
        payload["plan"] = plan
    return json.dumps(payload)


def _cleared(_root: Path, _site, patched: str) -> bool:
    return 'Store("orders")' not in patched and "api_key" in patched


def test_apply_transcripts_capture_exact_prompt_and_raw_response(tmp_path: Path):
    root = _acmekit_repo(tmp_path)
    sites = sites_from_payload(_acmekit_impact())
    reply = _edit()
    client = FakeModelClient(responses=[reply, "Renamed Store args."])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            site_cleared=_cleared,
        ),
    )
    data = report.to_dict()
    assert "transcripts" in data
    assert len(data["transcripts"]) == 1
    entry = data["transcripts"][0]
    assert entry["path"] == "mod.py"
    assert entry["line"] == 4
    assert entry["attempt"] == 1
    assert entry["response"] == reply
    assert entry["plan"] == ""
    assert entry["model"] == _settings().model
    assert entry["enable_thinking"] is False
    assert entry["prompt_tokens"] > 0
    assert entry["completion_tokens"] > 0
    prompt = entry["prompt"]
    assert isinstance(prompt, list) and len(prompt) == 2
    assert prompt[0]["role"] == "system"
    assert prompt[1]["role"] == "user"
    expected = [
        {"role": role, "content": content}
        for role, content in propose_messages(
            report.results[0].context  # type: ignore[arg-type]
        )
    ]
    assert prompt == expected


def test_apply_transcript_stores_plan_and_still_splices(tmp_path: Path):
    root = _acmekit_repo(tmp_path)
    sites = sites_from_payload(_acmekit_impact())
    plan = "rename name to api_key and pass host"
    reply = _edit(plan=plan)
    client = FakeModelClient(responses=[reply, "Updated Store constructor."])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            site_cleared=_cleared,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_UNVERIFIED
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert 'acmekit.Store(api_key="orders", host="local")' in text
    assert 'Store("orders")' not in text
    entry = report.to_dict()["transcripts"][0]
    assert entry["plan"] == plan
    assert entry["response"] == reply


def test_apply_without_plan_still_applies_with_empty_plan(tmp_path: Path):
    root = _acmekit_repo(tmp_path)
    sites = sites_from_payload(_acmekit_impact())
    reply = _edit()  # today's shape: no plan key
    assert "plan" not in json.loads(reply)
    client = FakeModelClient(responses=[reply, "Store args updated."])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            site_cleared=_cleared,
        ),
    )
    (result,) = report.results
    assert result.status == SiteStatus.FIXED_UNVERIFIED
    assert result.error == ""
    entry = report.to_dict()["transcripts"][0]
    assert entry["plan"] == ""
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert "api_key" in text


def test_transcript_controls_status_cap_and_no_api_keys(tmp_path: Path):
    root = _acmekit_repo(tmp_path)
    sites = sites_from_payload(_acmekit_impact())
    huge = "X" * (TRANSCRIPT_TEXT_MAX + 500)
    reply = _edit(
        plan="keep rewrite short",
        body=f'def run():\n    # {huge}\n    return acmekit.Store(api_key="orders", host="local")\n',
    )
    assert len(reply) > TRANSCRIPT_TEXT_MAX
    client = FakeModelClient(responses=[reply, "why"])
    report = apply_sites(
        sites,
        ApplyConfig(
            root=root,
            settings=_settings(),
            write=True,
            client=client,
            site_cleared=_cleared,
        ),
    )
    (result,) = report.results
    # Controls: rewrite status and edited source are unchanged by transcripts.
    assert result.status == SiteStatus.FIXED_UNVERIFIED
    text = (root / "mod.py").read_text(encoding="utf-8")
    assert 'acmekit.Store(api_key="orders", host="local")' in text
    assert 'Store("orders")' not in text

    entry = report.to_dict()["transcripts"][0]
    assert len(entry["response"]) <= TRANSCRIPT_TEXT_MAX + 80
    assert "chars omitted" in entry["response"]
    for message in entry["prompt"]:
        assert len(message["content"]) <= TRANSCRIPT_TEXT_MAX + 80

    blob = json.dumps(report.to_dict())
    assert "test-key-not-for-logging" not in blob
    assert "NEBIUS_API_KEY" not in blob
    assert "Authorization" not in blob
