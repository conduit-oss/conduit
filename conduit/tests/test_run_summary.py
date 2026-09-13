"""Run summary: decision-ready sections + surface sync helpers."""

from __future__ import annotations

from pathlib import Path

from conduit.anticheat.audit_log import MigrationAuditLog
from conduit.detect.client_state import PackageClientState
from conduit.detect.coverage import build_coverage_report
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai import OpenAIModule
from conduit.patcher.engine import ChangeRecord, PatchReport
from conduit.patcher.impact.engine import ImpactReport
from conduit.patcher.surface_sync import sync_surfaces
from conduit.pr_generator import build_pr_body
from conduit.run_summary import (
    build_run_summary,
    format_run_summary,
    format_run_summary_markdown,
)
from conduit.self_correct import reject_self_correct_write
from conduit.surface_paths import is_prose_ops_rel
from conduit.test_gen import oracle_scan_rels
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
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            },
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "text-davinci-003",
                "replace": "gpt-5.6-terra",
            },
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
    assert "Auto-updating deprecated models:" in joined
    assert "gpt-4-0613 → gpt-4o" in joined
    assert "Models added (not in client baseline): gpt-4o" in joined
    assert "confirm these are the models you want" in joined


def test_openai_review_includes_shutdown_from_rule_reason():
    packet = _packet()
    for rule in packet["rules"]:
        if rule["match"] == "gpt-4-0613":
            rule["reason"] = (
                "Auto-updating deprecated model gpt-4-0613 → gpt-4o "
                "(shutdown 2026-10-23)."
            )
    items = OpenAIModule().review_checklist(
        packet=packet,
        report=PatchReport(),
        state=PackageClientState(package="openai", model_ids=["gpt-4-0613"]),
    )
    joined = "\n".join(items)
    assert "shutdown 2026-10-23" in joined


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


def test_reject_self_correct_blocks_main_guard_and_renames():
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28",
        "to_version": "3.3.1",
        "rules": [],
    }
    previous = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def run():\n"
        "    return client.chat.completions.create(model='m', messages=[])\n"
        "bot_run = run\n"
        "bot_run()\n"
    )
    with_main = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def run():\n"
        "    return client.chat.completions.create(model='m', messages=[])\n"
        "if __name__ == '__main__':\n"
        "    bot_run = run\n"
        "    bot_run()\n"
    )
    renamed = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def run_bot():\n"
        "    return client.chat.completions.create(model='m', messages=[])\n"
        "bot_run = run_bot\n"
        "bot_run()\n"
    )
    ok = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def run():\n"
        "    return client.chat.completions.create(model='gpt-4o', messages=[])\n"
        "bot_run = run\n"
        "bot_run()\n"
    )
    with_token_if = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def run():\n"
        "    return client.chat.completions.create(model='m', messages=[])\n"
        "token = 'x'\n"
        "if token:\n"
        "    run()\n"
    )
    assert reject_self_correct_write(
        "app.py", with_main, packet=packet, previous=previous
    )
    assert "__main__" in (
        reject_self_correct_write("app.py", with_main, packet=packet, previous=previous)
        or ""
    )
    with_and_run = (
        "from openai import OpenAI\n"
        "client = OpenAI()\n"
        "def run():\n"
        "    return client.chat.completions.create(model='m', messages=[])\n"
        "token = 'x'\n"
        "token and bot.run(token)\n"
    )
    previous_run = previous + "bot.run(getenv('DISCORD_BOT_TOKEN'))\n"
    assert "module-level if" in (
        reject_self_correct_write(
            "app.py", with_token_if, packet=packet, previous=previous
        )
        or ""
    )
    assert reject_self_correct_write(
        "app.py", with_and_run, packet=packet, previous=previous_run
    )
    assert reject_self_correct_write(
        "app.py", renamed, packet=packet, previous=previous
    )
    assert (
        reject_self_correct_write("app.py", ok, packet=packet, previous=previous)
        is None
    )


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
    report.skips.append("[sdk] deferred impact path services/azure_bridge/__init__.py")
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
    impact = ImpactReport(
        defer_paths={"services/azure_bridge/__init__.py"},
        findings=[
            {
                "path": "services/azure_bridge/__init__.py",
                "kind": "azure_classic_client",
                "action": "fix",
            }
        ],
        post_rules=[{"type": "FUNCTION_BODY_REPLACE"}],
    )
    audit = MigrationAuditLog.from_packet(packet)
    audit.score = {
        "honesty": 100,
        "migration_completeness": 68,
        "notes": ["Deferred Azure bridge still needs a human look."],
    }
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
        audit_log=audit,
        impact=impact,
        docs_synced=["README.md", "docs/CHANGELOG.md"],
    )
    text = format_run_summary(summary)
    assert "Result: PASSED" in text
    assert "Impact" in text
    assert "services/azure_bridge/__init__.py" in text
    assert "fixed:" in text
    assert "azure_classic_client" in text
    assert "deferred (no fix applied)" not in text
    assert "Anti-cheat" in text
    assert "honesty=100" in text
    assert "Deferred Azure bridge" in text
    assert "Gaps (coverage)" in text
    assert "mystery-model" in text
    assert "Changed" in text
    assert "docs sync: README.md" in text
    assert "Next" in text
    assert "PR not opened:" in text
    assert "Package notes (openai)" in text
    assert "Models added (not in client baseline): gpt-4o" in text

    md = format_run_summary_markdown(summary)
    assert "### Review" in md
    assert "#### Impact" in md
    assert "#### Anti-cheat" in md
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


def test_run_summary_includes_side_effects():
    packet = _packet()
    packet["side_effects"] = [
        {
            "kind": "webhook",
            "detail": "Receivers must accept max_completion_tokens in the payload.",
        },
        {
            "kind": "database",
            "detail": "Migrate stored completion param name if persisted.",
        },
    ]
    summary = build_run_summary(
        packet=packet,
        report=PatchReport(),
        test_result=_tests(),
    )
    text = format_run_summary(summary)
    assert "Side effect (webhook): Receivers must accept max_completion_tokens" in text
    assert "Side effect (database): Migrate stored completion param name" in text
    md = format_run_summary_markdown(summary)
    assert "Side effect (webhook):" in md
    body = build_pr_body(
        packet,
        PatchReport(),
        _tests(),
        review_markdown=md,
    )
    assert "Receivers must accept max_completion_tokens" in body


def test_oracle_scan_excludes_prose_ops(tmp_path: Path):
    (tmp_path / "README.md").write_text("text-davinci-003\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "runbook.md").write_text("davinci\n", encoding="utf-8")
    (tmp_path / "scripts" / "ops").mkdir(parents=True)
    (tmp_path / "scripts" / "ops" / "smoke.sh").write_text("davinci\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("davinci\n", encoding="utf-8")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "models.yaml").write_text("model: text-davinci-003\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")
    rels = oracle_scan_rels(
        tmp_path,
        _packet(),
        file_allowlist=[tmp_path / "app.py"],
    )
    assert "app.py" in rels
    assert "configs/models.yaml" in rels
    assert "README.md" not in rels
    assert "docs/runbook.md" not in rels
    assert "scripts/ops/smoke.sh" not in rels
    assert "Dockerfile" not in rels


def test_self_correct_rejects_docs_write():
    reason = reject_self_correct_write(
        "docs/CHANGELOG.md",
        "# notes\n",
        packet=_packet(),
    )
    assert reason is not None
    assert "prose/ops" in reason
    reason2 = reject_self_correct_write(
        "README.md",
        "hi\n",
        packet=_packet(),
    )
    assert reason2 is not None


def test_is_prose_ops_rel():
    assert is_prose_ops_rel("docs/a.md")
    assert is_prose_ops_rel("README.md")
    assert is_prose_ops_rel("scripts/ops/foo.sh")
    assert not is_prose_ops_rel("packages/openai_text/chat.py")
    assert not is_prose_ops_rel("configs/models.yaml")


def test_impact_unfixed_defer_only():
    impact = ImpactReport(
        defer_paths={"services/mystery/bridge.py"},
        findings=[
            {
                "path": "services/mystery/bridge.py",
                "kind": "unknown_bridge",
                "action": "defer",
            }
        ],
        post_rules=[],
    )
    summary = build_run_summary(
        packet=_packet(),
        report=PatchReport(),
        test_result=_tests(),
        impact=impact,
    )
    text = format_run_summary(summary)
    assert "deferred (no fix applied): services/mystery/bridge.py" in text
    assert "fixed:" not in text


def test_surface_sync_clears_readme_token(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "Use text-davinci-003 for completions.\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")
    report = sync_surfaces(tmp_path, _packet())
    assert "README.md" in report.files_modified
    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "text-davinci-003" not in text
    assert "gpt-5.6-terra" in text
