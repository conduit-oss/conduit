"""Endpoint path → request param compat (OpenAPI pair diffs)."""

from __future__ import annotations

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.normalize import default_rules_for
from conduit.detect.modules.openai.models_legacy import ChangeType
from conduit.detect.modules.openai.path_param_compat import apply_path_param_compat
from conduit.detect.modules.openai.workers.base import fixtures_dir
from conduit.detect.modules.openai.workers.deprecation_scraper import (
    DeprecationScraperWorker,
)
from conduit.detect.modules.openai.workers.openapi_diff import (
    _diff_paths,
    _load_openapi,
    diff_path_params,
)


def test_diff_path_params_shared_rename():
    prev = _load_openapi(fixtures_dir() / "openapi" / "previous.yaml")
    latest = _load_openapi(fixtures_dir() / "openapi" / "latest.yaml")
    diff = diff_path_params(prev, latest, "/v1/chat/completions", "/v1/chat/completions")
    assert diff.renames == [("max_tokens", "max_completion_tokens")]
    assert diff.removed == []
    assert diff.added == []


def test_diff_path_params_cross_path_rename():
    prev = _load_openapi(fixtures_dir() / "openapi" / "previous.yaml")
    latest = _load_openapi(fixtures_dir() / "openapi" / "latest.yaml")
    diff = diff_path_params(prev, latest, "/v1/completions", "/v1/chat/completions")
    assert ("max_tokens", "max_completion_tokens") in diff.renames


def test_diff_path_params_removed_only_no_invent():
    previous = {
        "paths": {
            "/v1/old": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "properties": {"a": {}, "b": {}, "c": {}}
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    latest = {
        "paths": {
            "/v1/new": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"properties": {"a": {}, "x": {}, "y": {}}}
                            }
                        }
                    }
                }
            }
        }
    }
    diff = diff_path_params(previous, latest, "/v1/old", "/v1/new")
    assert diff.renames == []
    assert set(diff.removed) == {"b", "c"}
    assert set(diff.added) == {"x", "y"}


def test_shared_path_openapi_emits_function_target_and_rules():
    prev = _load_openapi(fixtures_dir() / "openapi" / "previous.yaml")
    latest = _load_openapi(fixtures_dir() / "openapi" / "latest.yaml")
    signals = _diff_paths(prev, latest)
    rename = [s for s in signals if s.affected_pattern == "max_tokens"]
    assert rename
    assert rename[0].extra.get("function_target")
    assert "chat.completions.create" in (rename[0].extra.get("function_targets") or [])
    rules = default_rules_for(rename[0])
    assert any(r.get("type") == "AST_PARAM_RENAME" for r in rules)
    assert any(r.get("reason") for r in rules)


def test_engines_removal_no_param_rename_rules():
    prev = _load_openapi(fixtures_dir() / "openapi" / "previous.yaml")
    latest = _load_openapi(fixtures_dir() / "openapi" / "latest.yaml")
    signals = _diff_paths(prev, latest)
    engines = [s for s in signals if s.affected_pattern == "/v1/engines"]
    assert engines
    assert engines[0].replacement_pattern is None
    assert default_rules_for(engines[0]) == []


def test_deprecation_fixture_emits_endpoint_pair():
    signals = DeprecationScraperWorker().run(demo=True)
    endpoints = [
        s
        for s in signals
        if s.change_type == ChangeType.API_BREAKING
        and s.affected_pattern == "/v1/completions"
    ]
    assert endpoints
    assert endpoints[0].replacement_pattern == "/v1/chat/completions"


def test_apply_path_param_compat_cross_path():
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="API_BREAKING",
            affected_pattern="/v1/completions",
            replacement_pattern="/v1/chat/completions",
            description="Endpoint /v1/completions deprecated",
            source_url="https://platform.openai.com/docs/deprecations",
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "target_files": ["*.py"],
                    "match": "/v1/completions",
                    "replace": "/v1/chat/completions",
                }
            ],
        ),
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="API_BREAKING",
            affected_pattern="/v1/engines",
            replacement_pattern=None,
            description="OpenAPI path removed: /v1/engines",
        ),
    ]
    state = PackageClientState(
        package="openai",
        api_patterns=["chat.completions"],
    )
    out, notes = apply_path_param_compat(signals, client_state=state, demo=True)
    param = [
        s
        for s in out
        if s.change_type == "PARAM_RENAME" and s.affected_pattern == "max_tokens"
    ]
    assert param
    assert param[0].replacement_pattern == "max_completion_tokens"
    assert param[0].suggested_rules
    assert any("chat.completions.create" in (r.get("function_target") or "") for r in param[0].suggested_rules)
    assert any("/v1/completions" in (n or "") for n in notes)
    # engines removal noted, no invented param rules for it
    assert any("engines" in (n or "") and "no documented successor" in (n or "") for n in notes)
