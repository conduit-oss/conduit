"""Verify/self-correct must not treat skip, dummy helpers, or token theater as success."""

from __future__ import annotations

from pathlib import Path

from conduit.credentials import (
    CredentialsError,
    ensure_verify_credentials,
    needs_openai_consumer_key,
)
from conduit.integrity import integrity_findings, unused_marker_literals
from conduit.patcher.engine import apply_packet
from conduit.repair_ignore import build_ignore_list
from conduit.self_correct import reject_self_correct_write
from conduit.test_gen import ensure_tests, oracle_scan_rels
from conduit.test_runner import (
    TestResult,
    evaluate_pytest_result,
    parse_pytest_counts,
    run_tests,
)


def _packet() -> dict:
    return {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "ada",
                "replace": "text-embedding-3-small",
                "target_files": ["*.py"],
            },
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
                "target_files": ["*.py"],
            },
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "/v1/fine-tunes",
                "replace": "/v1/fine_tuning/jobs",
                "target_files": ["*.py"],
            },
        ],
    }


def test_parse_pytest_skip_all_is_failure():
    passed, reason, counts = evaluate_pytest_result(
        returncode=0,
        stdout="===== 5 skipped in 0.04s =====\n",
        stderr="",
    )
    assert passed is False
    assert reason == "all tests skipped"
    assert counts["skipped"] == 5
    assert counts["passed"] == 0


def test_parse_pytest_missing_key_is_failure():
    passed, reason, _counts = evaluate_pytest_result(
        returncode=0,
        stdout="SKIPPED tests/test_chat.py: OPENAI_API_KEY (or OPENAI_KEY) is not set\n"
        "===== 3 skipped in 0.01s =====\n",
        stderr="",
    )
    assert passed is False
    assert "OPENAI_API_KEY" in reason


def test_parse_pytest_passed_with_skips_ok():
    passed, reason, counts = evaluate_pytest_result(
        returncode=0,
        stdout="===== 2 passed, 1 skipped in 0.10s =====\n",
        stderr="",
    )
    assert passed is True
    assert reason == ""
    assert counts["passed"] == 2


def test_parse_pytest_counts_helper():
    counts = parse_pytest_counts("===== 1 failed, 2 passed, 3 skipped, 1 error in 1s =====")
    assert counts["failed"] == 1
    assert counts["passed"] == 2
    assert counts["skipped"] == 3
    assert counts["error"] == 1


def test_run_tests_skip_all_conftest(tmp_path: Path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "import os, pytest\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def openai_configured():\n"
        "    if not os.environ.get('OPENAI_API_KEY'):\n"
        "        pytest.skip('OPENAI_API_KEY (or OPENAI_KEY) is not set')\n",
        encoding="utf-8",
    )
    (tests / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    result = run_tests(tmp_path)
    assert result.passed is False
    assert "skip" in (result.fail_reason or "").lower() or "OPENAI_API_KEY" in (
        result.fail_reason or ""
    )


def test_credentials_noninteractive_missing_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text(
        "OPENAI_API_KEY = True\n", encoding="utf-8"
    )
    try:
        ensure_verify_credentials(
            tmp_path, {"package": "openai"}, want_llm=False, interactive=False
        )
        raise AssertionError("expected CredentialsError")
    except CredentialsError as exc:
        assert "OPENAI_API_KEY" in str(exc)


def test_credentials_prompt_sets_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    ensure_verify_credentials(
        tmp_path,
        {"package": "openai"},
        want_llm=False,
        interactive=True,
        prompt=lambda _label: "sk-from-prompt",
    )
    assert monkeypatch is not None
    import os

    assert os.environ.get("OPENAI_API_KEY") == "sk-from-prompt"


def test_needs_key_from_conftest(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("os.environ.get('OPENAI_API_KEY')\n", encoding="utf-8")
    assert needs_openai_consumer_key(tmp_path, {"package": "demo"}) is True
    assert needs_openai_consumer_key(tmp_path, {"package": "openai"}) is True


def test_integrity_dummy_except(tmp_path: Path):
    (tmp_path / "chat.py").write_text(
        "def generate_reply(message):\n"
        "    try:\n"
        "        return call()\n"
        "    except Exception:\n"
        "        return message.strip() or 'ok'\n",
        encoding="utf-8",
    )
    findings = integrity_findings(tmp_path, _packet(), ["chat.py"])
    assert any("dummy" in f or "swallows Exception" in f for f in findings)


def test_integrity_migration_markers(tmp_path: Path):
    (tmp_path / "client.py").write_text(
        "MIGRATION_MARKERS = (\n"
        "    'chat.completions.create',\n"
        "    'gpt-realtime-2.1',\n"
        ")\n",
        encoding="utf-8",
    )
    findings = integrity_findings(tmp_path, _packet(), ["client.py"])
    assert any("marker" in f.lower() for f in findings)
    hidden = unused_marker_literals(
        (tmp_path / "client.py").read_text(encoding="utf-8"),
        ["chat.completions.create"],
    )
    assert hidden


def test_join_obfuscation_in_impl_not_ignored(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "none")
    app = tmp_path / "completions.py"
    app.write_text('LEGACY_ADA = "".join(["a", "da"])\n', encoding="utf-8")
    packet = _packet()
    ignore = build_ignore_list(tmp_path, packet)
    assert not ignore.path_ignored("completions.py")
    rels = oracle_scan_rels(tmp_path, packet, file_allowlist=[app], ignore=ignore)
    assert "completions.py" in rels
    reason = reject_self_correct_write("completions.py", app.read_text(encoding="utf-8"), packet=packet)
    assert reason is not None
    assert "obfuscat" in reason


def test_packet_apply_skips_generated_oracle(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    oracle = tests / "test_conduit_oracle.py"
    original = 'FORBIDDEN = ["ada", "gpt-4"]\n'
    oracle.write_text(original, encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 'ok'\n", encoding="utf-8")
    apply_packet(tmp_path, _packet(), require_context=False)
    assert oracle.read_text(encoding="utf-8") == original


def test_reject_skip_and_smoke_edits():
    packet = _packet()
    assert reject_self_correct_write(
        "tests/test_app.py",
        "import pytest\npytest.skip('nope')\n",
        packet=packet,
    )
    assert reject_self_correct_write(
        "tests/test_conduit_smoke.py",
        "def test_x():\n    assert True\n",
        packet=packet,
    )
    assert reject_self_correct_write(
        "tests/test_conduit_functional.py",
        "def test_x():\n    assert True\n",
        packet=packet,
    )


def test_run_tests_no_suite_is_failure(tmp_path: Path):
    result = run_tests(tmp_path)
    assert isinstance(result, TestResult)
    assert result.passed is False
    assert "no test suite" in result.fail_reason
