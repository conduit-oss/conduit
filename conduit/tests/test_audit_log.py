"""Migration audit log + log-first auditor wiring."""

from __future__ import annotations

from pathlib import Path

from conduit.anticheat.audit_log import MigrationAuditLog, redact_secrets
from conduit.anticheat.scan import run_anticheat
from conduit.patcher.engine import ChangeRecord, PatchReport


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
                "match": "/v1/engines",
                "replace": "/v1/models",
            },
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
            },
        ],
    }


def test_redact_secrets():
    assert "[REDACTED]" in redact_secrets("key=sk-abc1234567890xyz")
    assert "[REDACTED]" in redact_secrets("OPENAI_API_KEY=secret-value")


def test_audit_log_records_apply_reject_restore(tmp_path: Path):
    log = MigrationAuditLog.from_packet(_packet(), root=tmp_path)
    report = PatchReport()
    report.add(
        ChangeRecord(
            event_id="1",
            path="chat.py",
            rule_type="AST_CALL_REWRITE",
            detail="ChatCompletion -> chat.completions",
        )
    )
    log.record_apply(report)
    log.record_reject("client.py", "fake client", attempt=1)
    log.record_write(
        "client.py",
        before="import openai\n",
        after="import openai\nopenai.chat = X\n",
        attempt=1,
    )
    log.record_restore(["client.py"], attempt=1)
    assert any(e["phase"] == "apply" for e in log.entries)
    assert any(e["phase"] == "reject" for e in log.entries)
    assert any(e["phase"] == "write" for e in log.entries)
    assert any(e["phase"] == "restore" for e in log.entries)
    path = log.persist(tmp_path)
    assert path.is_file()
    loaded = MigrationAuditLog.load(tmp_path)
    assert loaded is not None
    assert len(loaded.entries) == len(log.entries)


def test_for_llm_truncates_and_lists_paths():
    log = MigrationAuditLog.from_packet(_packet())
    for i in range(5):
        log.record_write(f"f{i}.py", before="a", after="b" * 100)
    log.record_reject("evil.py", "shim")
    payload = log.for_llm(max_entries=10, max_chars=50_000)
    assert "path_allowlist" in payload
    assert "evil.py" in payload["path_allowlist"]
    assert "entries" in payload


def test_repair_journal_summarizes_prior_attempts():
    log = MigrationAuditLog.from_packet(_packet())
    log.record_write(
        "client.py",
        before="old",
        after="bad",
        attempt=1,
        detail="attempt 1 write",
    )
    log.record_reject("shim.py", "fake client", attempt=1)
    log.record_restore(["client.py"], attempt=1, detail="regressed")
    log.record_write("client.py", before="old", after="better", attempt=2)
    journal = log.repair_journal(attempt=2)
    phases = {e["phase"] for e in journal["entries"]}
    assert "write" in phases
    assert "reject" in phases
    assert "restore" in phases
    assert all(
        e.get("attempt") is None or int(e.get("attempt") or 0) < 2
        for e in journal["entries"]
    )
    reject = next(e for e in journal["entries"] if e["phase"] == "reject")
    assert "fake client" in (reject.get("reason") or reject.get("detail") or "")


def test_auditor_uses_log_and_score_non_gating(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text("from openai import OpenAI\n", encoding="utf-8")
    audit = MigrationAuditLog.from_packet(_packet(), root=tmp_path)
    audit.record_write(
        "app.py",
        before="import openai\n",
        after="from openai import OpenAI\n",
    )

    def _fake(*_a, **_k):
        return [], {
            "honesty": 88,
            "migration_completeness": 91,
            "notes": ["looks clean"],
        }

    monkeypatch.setattr("conduit.anticheat.llm_audit.llm_audit_findings", _fake)
    report = run_anticheat(
        tmp_path, _packet(), llm=True, audit_log=audit
    )
    assert not report.failed
    assert report.score and report.score["honesty"] == 88
    assert audit.score and audit.score["honesty"] == 88


def test_auditor_cheat_from_log_entry(tmp_path: Path, monkeypatch):
    (tmp_path / "client.py").write_text("import openai\n", encoding="utf-8")
    audit = MigrationAuditLog.from_packet(_packet(), root=tmp_path)
    audit.record_write(
        "client.py",
        after="import openai\nclass _CompatChat:\n    pass\nopenai.chat = _CompatChat()\n",
    )

    def _fake(*_a, **_k):
        return [
            "client.py: monkeypatch — Compat wrapper [e1]"
        ], {"honesty": 20, "migration_completeness": 50, "notes": []}

    monkeypatch.setattr("conduit.anticheat.llm_audit.llm_audit_findings", _fake)
    report = run_anticheat(
        tmp_path, _packet(), llm=True, audit_log=audit
    )
    assert report.failed
    assert any("monkeypatch" in f.lower() for f in report.findings)
