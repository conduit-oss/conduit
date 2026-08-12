"""Run summary: changed vs double-check (core + package)."""

from __future__ import annotations

from conduit.detect.client_state import PackageClientState
from conduit.detect.coverage import build_coverage_report
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai import OpenAIModule
from conduit.patcher.engine import ChangeRecord, PatchReport
from conduit.pr_generator import build_pr_body
from conduit.run_summary import (
    build_run_summary,
    format_run_summary,
    format_run_summary_markdown,
)
from conduit.test_runner import TestResult as RunnerResult


def _tests(*, passed: bool = True) -> RunnerResult:
    return RunnerResult(
        runner="pytest",
        passed=passed,
        returncode=0 if passed else 1,
        stdout="",
        stderr="",
        command=["pytest", "-q"],
    )


def _packet() -> dict:
    return {
        "packet_id": "openai-0.28.1-1.0.0",
        "package": "openai",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            }
        ],
    }


def test_openai_review_flags_added_models():
    state = PackageClientState(
        package="openai",
        model_ids=["gpt-4-0613"],
    )
    items = OpenAIModule().review_checklist(
        packet=_packet(),
        report=PatchReport(),
        state=state,
    )
    joined = "\n".join(items)
    assert "gpt-4-0613 → gpt-4o" in joined
    assert "Models added (not in client baseline): gpt-4o" in joined
    assert "confirm these are the models you want" in joined


def test_openai_review_no_added_when_replacement_already_used():
    state = PackageClientState(
        package="openai",
        model_ids=["gpt-4-0613", "gpt-4o"],
    )
    items = OpenAIModule().review_checklist(
        packet=_packet(),
        report=PatchReport(),
        state=state,
    )
    joined = "\n".join(items)
    assert "gpt-4-0613 → gpt-4o" in joined
    assert "Models added" not in joined


def test_run_summary_core_and_package_sections():
    packet = _packet()
    report = PatchReport()
    report.add(
        ChangeRecord(
            event_id="e1",
            path="src/ai_client.py",
            rule_type="EXACT_STRING_REPLACE",
            detail="`gpt-4-0613` → `gpt-4o`",
        )
    )
    state = PackageClientState(
        package="openai",
        model_ids=["gpt-4-0613", "mystery-model"],
        api_patterns=["chat.completions"],
        import_files=["src/ai_client.py"],
    )
    coverage = build_coverage_report(
        package="openai",
        state=state,
        signals=[
            ChangeSignal(
                source="module:openai",
                package="openai",
                change_type="MODEL_DEPRECATION",
                affected_pattern="gpt-4-0613",
                replacement_pattern="gpt-4o",
                suggested_rules=packet["rules"],
            )
        ],
        packet=packet,
    )
    summary = build_run_summary(
        packet=packet,
        report=report,
        test_result=_tests(),
        coverage=coverage,
        state=state,
        generated=["tests/test_conduit_smoke.py"],
        corrected=["src/ai_client.py"],
        pr_created=False,
        pr_message="gh not found; install it or use --skip-pr",
    )
    text = format_run_summary(summary)
    assert "Changed (core)" in text
    assert "openai-0.28.1-1.0.0" in text
    assert "src/ai_client.py" in text
    assert "Double-check (core)" in text
    assert "mystery-model" in text
    assert "Review generated tests: tests/test_conduit_smoke.py" in text
    assert "Review self-correct edits: src/ai_client.py" in text
    assert "PR not opened:" in text
    assert "Double-check (openai)" in text
    assert "Models added (not in client baseline): gpt-4o" in text

    md = format_run_summary_markdown(summary)
    assert "### Review" in md
    assert "#### Core" in md
    assert "#### openai" in md


def test_build_pr_body_includes_review_section():
    body = build_pr_body(
        _packet(),
        PatchReport(),
        _tests(),
        review_markdown="### Review\n\n#### openai\n- Confirm replacement models",
    )
    assert "### Review" in body
    assert "Confirm replacement models" in body
