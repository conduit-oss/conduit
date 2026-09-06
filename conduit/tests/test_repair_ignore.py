"""Tests for dynamic repair ignore lists."""

from __future__ import annotations

from pathlib import Path

from conduit.repair_ignore import (
    build_ignore_list,
    exact_replace_respecting_ignore,
    line_is_contract_assignment,
)
from conduit.self_correct import _heuristic_fix


def test_discover_policy_file_ignored(tmp_path: Path):
    src = tmp_path / "src" / "app"
    src.mkdir(parents=True)
    (src / "policy.py").write_text(
        'LEGACY_CHAT_PARAM = "max_tokens"\nFORBIDDEN_ENDPOINTS = {"/v1/engines"}\n',
        encoding="utf-8",
    )
    (src / "chat.py").write_text(
        'def f():\n    return {"max_tokens": 64}\n',
        encoding="utf-8",
    )
    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "max_tokens",
                "replace": "max_completion_tokens",
                "target_files": ["*.py"],
            }
        ]
    }
    ignore = build_ignore_list(tmp_path, packet)
    assert ignore.path_ignored("src/app/policy.py")
    assert not ignore.path_ignored("src/app/chat.py")


def test_heuristic_skips_ignored_policy_file(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    policy = src / "policy.py"
    policy.write_text('LEGACY_CHAT_PARAM = "max_tokens"\n', encoding="utf-8")
    chat = src / "chat.py"
    chat.write_text('x = "max_tokens"\n', encoding="utf-8")
    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "max_tokens",
                "replace": "max_completion_tokens",
                "target_files": ["*.py"],
            }
        ],
        "ignore": {"paths": ["src/policy.py"]},
    }
    fix = _heuristic_fix(tmp_path, packet, ignore=build_ignore_list(tmp_path, packet))
    assert "src/chat.py" in fix.files or any("chat.py" in f for f in fix.files)
    assert "max_tokens" in policy.read_text(encoding="utf-8")
    assert "max_completion_tokens" in chat.read_text(encoding="utf-8")


def test_contract_line_spared_inside_mixed_file():
    content = (
        'LEGACY_CHAT_PARAM = "max_tokens"\n'
        'payload = {"max_tokens": 64}\n'
    )
    out, n = exact_replace_respecting_ignore(
        content,
        "max_tokens",
        "max_completion_tokens",
        ignored_patterns={"max_tokens"},
    )
    assert n == 1
    assert 'LEGACY_CHAT_PARAM = "max_tokens"' in out
    assert '"max_completion_tokens"' in out
    assert line_is_contract_assignment('LEGACY_CHAT_PARAM = "max_tokens"', "max_tokens")


def test_conduit_ignore_json(tmp_path: Path):
    (tmp_path / ".conduit").mkdir()
    (tmp_path / ".conduit" / "ignore.json").write_text(
        '{"globs": ["**/oracle.py"]}',
        encoding="utf-8",
    )
    (tmp_path / "oracle.py").write_text("x = 1\n", encoding="utf-8")
    ignore = build_ignore_list(tmp_path, {"rules": []})
    assert ignore.path_ignored("oracle.py")


def test_heuristic_does_not_rewrite_conduit_oracle(tmp_path: Path):
    """match→replace must not poison FORBIDDEN/REQUIRED into successor identity pairs."""
    tests = tmp_path / "tests"
    tests.mkdir()
    oracle = tests / "test_conduit_oracle.py"
    oracle_body = (
        'FORBIDDEN = ["text-davinci-003", "/v1/engines"]\n'
        "REQUIRED = [\n"
        '    {"kind": "replace", "old": "text-davinci-003", "new": "gpt-5.6-terra"},\n'
        '    {"kind": "replace", "old": "/v1/engines", "new": "/v1/models"},\n'
        "]\n"
    )
    oracle.write_text(oracle_body, encoding="utf-8")
    smoke = tests / "test_conduit_smoke.py"
    smoke_body = 'REQUIRED = [{"old": "text-davinci-003", "new": "gpt-5.6-terra"}]\n'
    smoke.write_text(smoke_body, encoding="utf-8")
    app = tmp_path / "src"
    app.mkdir()
    (app / "chat.py").write_text('MODEL = "text-davinci-003"\n', encoding="utf-8")

    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "text-davinci-003",
                "replace": "gpt-5.6-terra",
            },
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "/v1/engines",
                "replace": "/v1/models",
            },
        ]
    }
    ignore = build_ignore_list(tmp_path, packet)
    assert ignore.path_ignored("tests/test_conduit_oracle.py")
    assert ignore.path_ignored("tests/test_conduit_smoke.py")

    fix = _heuristic_fix(tmp_path, packet, ignore=ignore)
    assert oracle.read_text(encoding="utf-8") == oracle_body
    assert smoke.read_text(encoding="utf-8") == smoke_body
    assert "tests/test_conduit_oracle.py" not in fix.files
    assert "tests/test_conduit_smoke.py" not in fix.files
    assert "gpt-5.6-terra" in (app / "chat.py").read_text(encoding="utf-8")


def test_heuristic_updates_configs_not_tests(tmp_path: Path):
    configs = tmp_path / "configs"
    configs.mkdir()
    registry = configs / "model_registry.json"
    registry.write_text('{"primary": "text-davinci-003"}\n', encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    test_cfg = tests / "test_configs.py"
    test_body = 'assert registry["primary"] == "text-davinci-003"\n'
    test_cfg.write_text(test_body, encoding="utf-8")
    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "text-davinci-003",
                "replace": "gpt-5.6-terra",
            }
        ]
    }
    fix = _heuristic_fix(tmp_path, packet, ignore=build_ignore_list(tmp_path, packet))
    assert "configs/model_registry.json" in fix.files
    assert "gpt-5.6-terra" in registry.read_text(encoding="utf-8")
    assert test_cfg.read_text(encoding="utf-8") == test_body
    assert "tests/test_configs.py" not in fix.files


def test_heuristic_skips_non_utf8_file(tmp_path: Path):
    """Binary/legacy-encoded siblings must not abort heuristic repair."""
    web = tmp_path / "web" / "static"
    web.mkdir(parents=True)
    bad = web / "vendor.min.js"
    # Invalid UTF-8 (same class of failure as rengine static assets).
    bad.write_bytes(b"\x88\x00binary text-davinci-003 junk")
    good = web / "app.js"
    good.write_text('const model = "text-davinci-003";\n', encoding="utf-8")
    packet = {
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "text-davinci-003",
                "replace": "gpt-5.6-terra",
            }
        ]
    }
    fix = _heuristic_fix(tmp_path, packet, ignore=build_ignore_list(tmp_path, packet))
    assert "web/static/app.js" in fix.files
    assert "gpt-5.6-terra" in good.read_text(encoding="utf-8")
    assert "web/static/vendor.min.js" not in fix.files
    assert bad.read_bytes().startswith(b"\x88")
