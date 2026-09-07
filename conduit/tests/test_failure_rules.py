"""Tests for verify-learned failure_rules."""

from __future__ import annotations

from conduit.packet.failure_rules import suggest_rules_from_failure
from conduit.test_runner import TestResult


def test_failure_rules_skips_when_no_call_target():
    result = TestResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout="BadRequestError: 'foo' does not support 0\n",
        stderr="",
    )
    assert suggest_rules_from_failure(result, packet={"rules": []}) == []


def test_failure_rules_uses_packet_call_rewrite_target():
    result = TestResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout="BadRequestError: Unsupported value: 'temperature' does not support 0\n",
        stderr="",
    )
    rules = suggest_rules_from_failure(
        result,
        packet={
            "rules": [
                {
                    "type": "AST_CALL_REWRITE",
                    "old_callee": "Completion.create",
                    "new_callee": "chat.completions.create",
                }
            ]
        },
    )
    assert rules[0]["function_target"] == "chat.completions.create"


def test_failure_rules_suggests_param_rename_from_api_error():
    result = TestResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=(
            "BadRequestError: Error code: 400 - Unsupported parameter: 'max_tokens' "
            "is not supported with this model. Use 'max_completion_tokens' instead."
        ),
        stderr="",
    )
    rules = suggest_rules_from_failure(
        result,
        packet={
            "rules": [
                {
                    "type": "AST_CALL_REWRITE",
                    "new_callee": "chat.completions.create",
                }
            ]
        },
    )
    rename = [r for r in rules if r.get("type") == "AST_PARAM_RENAME"]
    assert rename
    assert rename[0]["old_param"] == "max_tokens"
    assert rename[0]["new_param"] == "max_completion_tokens"
