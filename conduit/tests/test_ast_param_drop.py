"""AST_PARAM_DROP, failure_rules, and detect-driven migration (no catalog seeds)."""

from __future__ import annotations

from pathlib import Path

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.path_param_compat import apply_path_param_compat
from conduit.detect.modules.openai.sdk_callee_migration import apply_sdk_callee_migration
from conduit.detect.modules.openai.workers.openapi_diff import (
    _load_openapi,
    diff_path_params,
)
from conduit.detect.modules.openai.workers.base import fixtures_dir
from conduit.packet.failure_rules import suggest_rules_from_failure
from conduit.patcher.ast_param_drop import apply_param_drop, drop_python_params
from conduit.patcher.engine import apply_packet
from conduit.test_runner import TestResult


def test_drop_python_temperature_zero_only():
    src = (
        "client.chat.completions.create(\n"
        "    model='m',\n"
        "    messages=[],\n"
        "    temperature=0,\n"
        "    max_completion_tokens=16,\n"
        ")\n"
        "client.chat.completions.create(model='m', messages=[], temperature=0.7)\n"
    )
    out, n = drop_python_params(
        src,
        function_target="chat.completions.create",
        param="temperature",
        values=[0, 0.0],
    )
    assert n == 1
    assert "temperature=0," not in out
    assert "temperature=0.7" in out


def test_apply_packet_param_drop(tmp_path: Path):
    app = tmp_path / "app.py"
    app.write_text(
        "client.chat.completions.create(model='x', messages=[], temperature=0)\n",
        encoding="utf-8",
    )
    packet = {
        "packet_id": "t",
        "package": "openai",
        "rules": [
            {
                "type": "AST_PARAM_DROP",
                "target_files": ["*.py"],
                "function_target": "chat.completions.create",
                "param": "temperature",
                "values": [0, 0.0],
            }
        ],
    }
    report = apply_packet(tmp_path, packet, dry_run=False, require_context=False)
    assert report.changes
    assert "temperature" not in app.read_text(encoding="utf-8")


def test_openapi_shared_path_rename_without_seeds():
    prev = _load_openapi(fixtures_dir() / "openapi" / "previous.yaml")
    latest = _load_openapi(fixtures_dir() / "openapi" / "latest.yaml")
    diff = diff_path_params(prev, latest, "/v1/chat/completions", "/v1/chat/completions")
    assert ("max_tokens", "max_completion_tokens") in diff.renames


def test_path_param_compat_emits_param_drop_for_removed_props():
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="API_BREAKING",
            affected_pattern="/v1/completions",
            replacement_pattern="/v1/chat/completions",
            description="Endpoint /v1/completions deprecated",
            source_url="https://platform.openai.com/docs/deprecations",
        )
    ]
    previous = {
        "paths": {
            "/v1/completions": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"properties": {"legacy_flag": {}, "max_tokens": {}}}
                            }
                        }
                    }
                }
            }
        }
    }
    latest = {
        "paths": {
            "/v1/chat/completions": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"properties": {"max_completion_tokens": {}}}
                            }
                        }
                    }
                }
            }
        }
    }
    from conduit.detect.modules.openai.workers import openapi_diff as od

    od._OPENAPI_PAIR_CACHE["openai:demo"] = (previous, latest)
    try:
        out, notes = apply_path_param_compat(
            signals,
            client_state=PackageClientState(
                package="openai", api_patterns=["chat.completions"]
            ),
            demo=True,
        )
    finally:
        od._OPENAPI_PAIR_CACHE.pop("openai:demo", None)
    removed = [s for s in out if s.change_type == "PARAM_REMOVED"]
    assert removed
    assert any(
        r.get("type") == "AST_PARAM_DROP" and r.get("param") == "legacy_flag"
        for s in removed
        for r in s.suggested_rules
    )
    assert any("legacy_flag" in (n or "") for n in notes)


def test_sdk_callee_migration_from_endpoint_pair():
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="API_BREAKING",
            affected_pattern="/v1/completions",
            replacement_pattern="/v1/chat/completions",
            source_url="https://platform.openai.com/docs/deprecations",
        )
    ]
    state = PackageClientState(package="openai", api_patterns=["Completion.create"])
    out, notes = apply_sdk_callee_migration(signals, client_state=state)
    mig = [s for s in out if s.change_type == "SDK_CALLEE_MIGRATION"]
    assert mig
    rules = mig[0].suggested_rules
    assert any(
        r.get("type") == "AST_CALL_REWRITE"
        and r.get("old_callee") == "Completion.create"
        and r.get("new_callee") == "chat.completions.create"
        for r in rules
    )
    assert notes


def test_failure_rules_from_unsupported_value():
    result = TestResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=(
            "openai.BadRequestError: Unsupported value: 'temperature' "
            "does not support 0 with this model.\n"
        ),
        stderr="",
    )
    rules = suggest_rules_from_failure(
        result,
        file_windows=[
            {
                "path": "app.py",
                "text": "client.chat.completions.create(model='m', messages=[], temperature=0)\n",
            }
        ],
        packet={"rules": []},
    )
    assert len(rules) == 1
    assert rules[0]["type"] == "AST_PARAM_DROP"
    assert rules[0]["param"] == "temperature"
    assert rules[0]["values"] == [0]


def test_apply_param_drop_js_heuristic():
    src = "await client.chat.completions.create({ model: 'm', temperature: 0, max: 1 })\n"
    out, n = apply_param_drop(
        Path("x.ts"),
        src,
        function_target="chat.completions.create",
        param="temperature",
        values=[0, 0.0],
    )
    assert n >= 1
    assert "temperature" not in out
