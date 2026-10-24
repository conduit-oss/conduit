"""What a new-release callable requires reaches the prompt, read from its own code."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.apply import verify as verify_mod
from conduit.apply.client import FakeModelClient
from conduit.apply.engine import ApplyConfig, apply_sites
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.models import SiteStatus
from conduit.apply.signatures import _modules
from conduit.apply.typecheck import parameter_brief

from test_apply import (
    _good_edit,
    _hits_in_span,
    _impact_payload,
    _new_release_fixture,
    _repo_with_break,
    _settings,
)

_SHAPE = 'items: Iterable[ItemParam], i.e. a list like [{"name": <str>, "size": <int>}, ...]'


def test_parameter_brief_expands_required_typed_dict_parameters(tmp_path: Path):
    modules = _modules(str(_new_release_fixture(tmp_path) / "new"))
    brief = parameter_brief(modules, "example_sdk.Client.new_call")
    assert brief.startswith("Client.new_call requires: value: int; " + _SHAPE)
    assert "ItemParam is a TypedDict: name: str, size: int" in brief
    assert brief.endswith("Other parameters it accepts (no others): retries")
    assert parameter_brief(modules, "example_sdk.Client.loose").endswith("(**kwargs).")
    assert parameter_brief(modules, "example_sdk.Client.gone") == ""


def _removed_site_payload() -> dict:
    payload = _impact_payload()
    impact = payload["impacts"][0]
    impact.update(kind="removed", symbol="example_sdk.Client.old_call", old="example_sdk.Client.old_call", new="")
    impact["candidates"] = [
        {"symbol": "example_sdk.Client.new_call", "signature": "example_sdk.Client.new_call(value, *, items, re"},
        {"symbol": "example_sdk.client.Client.new_call", "signature": "same callable, re-exported"},
    ]
    return payload


def test_first_prompt_carries_what_a_replacement_candidate_requires(tmp_path: Path, monkeypatch):
    root = _repo_with_break(tmp_path)
    fixtures = _new_release_fixture(tmp_path)
    monkeypatch.setattr(verify_mod, "_breaks_listed", _hits_in_span)
    client = FakeModelClient(responses=[_good_edit(), "why"])
    apply_sites(
        sites_from_payload(_removed_site_payload()),
        ApplyConfig(root=root, settings=_settings(), client=client, recheck_impact=True, fixtures=fixtures),
    )
    first = client.calls[0]["messages"][-1].content
    pack = json.loads(first.split("Context pack (JSON):", 1)[1].rsplit("Return JSON", 1)[0])
    requires = pack["release_diff"]["new_release_requires"]
    # The re-export reads the same, so it is listed once.
    assert len(requires) == 1
    assert requires[0].startswith("Client.new_call requires: value: int; " + _SHAPE)


def test_recheck_retry_carries_what_the_called_callable_requires(tmp_path: Path, monkeypatch):
    root = _repo_with_break(tmp_path)
    fixtures = _new_release_fixture(tmp_path)
    payload = _impact_payload()
    payload["impacts"][0]["symbol"] = "example_sdk.Client.new_call"
    monkeypatch.setattr(verify_mod, "_breaks_listed", _hits_in_span)
    still_broken = json.dumps(
        {"function_source": "def broken(client):\n    return client.new_call(1, timeout=5)\n", "new_imports": []}
    )
    client = FakeModelClient(responses=[still_broken, _good_edit(), "why"])
    report = apply_sites(
        sites_from_payload(payload),
        ApplyConfig(root=root, settings=_settings(max_attempts=3), client=client, recheck_impact=True, fixtures=fixtures),
    )
    assert report.results[0].status == SiteStatus.FIXED_UNVERIFIED, report.results[0].error
    retry = client.calls[1]["messages"][-1].content
    assert "What it requires (read from its own code):" in retry
    assert _SHAPE in retry
