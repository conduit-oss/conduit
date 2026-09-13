"""Tests for anti-cheat validator, legal surfaces, and repair seeding."""

from __future__ import annotations

from pathlib import Path

from conduit.anticheat.findings import (
    AnticheatFinding,
    classify_mechanical_messages,
    parse_anticheat_failure_blob,
)
from conduit.anticheat.legal_surfaces import matches_legal_surface
from conduit.anticheat.scope import audit_scope_paths, is_migration_impl_rel
from conduit.anticheat.validator import validate_llm_cheats
from conduit.anticheat.scan import run_anticheat_mechanical, anticheat_failure_result
from conduit.self_correct import _anticheat_finding_hits, collect_repair_context
from conduit.test_runner import TestResult


def _stripe_packet() -> dict:
    return {
        "packet_id": "t",
        "package": "stripe",
        "ecosystem": "pypi",
        "from_version": "1",
        "to_version": "2",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "stripe.Charge.create",
                "new_callee": "PaymentIntent.create",
            }
        ],
    }


def test_legal_surface_accepts_official_stripe_client():
    text = (
        "from stripe import StripeClient\n\n"
        "def charge():\n"
        "    client = StripeClient()\n"
        "    return client.PaymentIntent.create(amount=100)\n"
    )
    assert matches_legal_surface(text, _stripe_packet())


def test_validator_drops_compat_wrapper_on_legal_surface(tmp_path: Path):
    rel = "billing.py"
    path = tmp_path / rel
    path.write_text(
        "from stripe import StripeClient\n\n"
        "def pay():\n"
        "    return StripeClient().PaymentIntent.create(amount=1)\n",
        encoding="utf-8",
    )
    cheat = AnticheatFinding(
        path=rel,
        kind="compat_wrapper",
        detail="looks like a wrapper",
        severity="block",
        source="llm",
        log_ref="e1",
    )
    block, advisory = validate_llm_cheats(
        [cheat],
        root=tmp_path,
        packet=_stripe_packet(),
        audit_log=None,
    )
    assert not block


def test_validator_keeps_fake_client_cheat(tmp_path: Path):
    rel = "billing.py"
    path = tmp_path / rel
    path.write_text(
        "class _FakeStripe:\n"
        "    def PaymentIntent(self):\n"
        "        return self\n"
        "    def create(self, **kw):\n"
        "        return {'ok': True}\n",
        encoding="utf-8",
    )
    cheat = AnticheatFinding(
        path=rel,
        kind="fake_client",
        detail="reimplements SDK",
        severity="block",
        source="llm",
    )
    block, _ = validate_llm_cheats(
        [cheat],
        root=tmp_path,
        packet=_stripe_packet(),
    )
    assert block


def test_echo_script_stub_is_advisory():
    msgs = [
        "scripts/ops/foo.sh echo-stubs API paths without migrating URLs/models "
        "(scripts must use real endpoints, not echo placeholders)"
    ]
    structured = classify_mechanical_messages(
        msgs,
        packet={"package": "openai", "ecosystem": "pypi"},
    )
    assert structured[0].severity == "advisory"


def test_block_only_failure_result():
    block = AnticheatFinding(
        path="a.py", kind="fake_client", detail="bad", severity="block"
    )
    advisory = AnticheatFinding(
        path="docs/x.md",
        kind="incomplete_migration",
        detail="wording",
        severity="advisory",
    )
    result = anticheat_failure_result([block], advisory=[advisory])
    assert not result.passed
    assert "a.py" in result.stdout
    assert "advisory:" in result.stdout


def test_mechanical_report_does_not_fail_on_advisory_only(tmp_path: Path):
    rel = "scripts/ops/foo.sh"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text('echo "/v1/models"\n', encoding="utf-8")
    # mechanical scan skips .sh in default iter — inject via file_findings path
    from conduit.anticheat.rules import echo_script_stub_finding

    msg = echo_script_stub_finding(rel, path.read_text(encoding="utf-8"))
    assert msg
    assert msg.startswith(f"{rel}:")
    structured = classify_mechanical_messages(
        [msg],
        packet={"package": "openai", "ecosystem": "pypi"},
    )
    assert structured[0].severity == "advisory"


def test_audit_scope_excludes_docs(tmp_path: Path):
    docs = tmp_path / "docs" / "runbook.md"
    docs.parent.mkdir(parents=True)
    docs.write_text("legacy davinci notes\n", encoding="utf-8")
    impl = tmp_path / "app.py"
    impl.write_text("import openai\nopenai.chat.completions.create()\n", encoding="utf-8")
    packet = {"package": "openai", "ecosystem": "pypi", "rules": []}
    scope = audit_scope_paths(tmp_path, packet)
    assert "app.py" in scope
    assert "docs/runbook.md" not in scope


def test_anticheat_failure_parsing_and_repair_seed(tmp_path: Path):
    rel = "pkg/chat.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    stdout = (
        "anticheat failed:\n"
        f"{rel}: compat_wrapper — thin wrapper [e9]\n"
    )
    hits = _anticheat_finding_hits(stdout)
    assert hits and hits[0][0] == rel
    result = TestResult(
        runner="anticheat",
        passed=False,
        returncode=1,
        stdout=stdout,
        stderr="",
        command=[],
    )
    ctx = collect_repair_context(tmp_path, result)
    assert rel in ctx.seeded_paths


def test_parse_anticheat_failure_blob():
    blob = "anticheat failed:\nfoo.py: parallel_http — curl used\n"
    findings = parse_anticheat_failure_blob(blob)
    assert len(findings) == 1
    assert findings[0].path == "foo.py"


def test_is_migration_impl_rel():
    packet = {"package": "openai", "ecosystem": "pypi"}
    assert is_migration_impl_rel("app.py", "import openai\n", packet)
    assert not is_migration_impl_rel("README.md", "openai", packet)
