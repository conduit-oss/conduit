"""Run-start preflight warnings for skipped LLM and pipeline steps."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from rich.console import Console

from conduit.credentials import load_consumer_env
from conduit.detect.client_state import PackageClientState, _agent_enrich
from conduit.detect.orchestrator import run_detect
from conduit.packet.synthesize import ensure_packet
from conduit.run_preflight import (
    collect_run_preflight_warnings,
    describe_llm_status,
    print_run_preflight,
)


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)


def test_collect_warnings_published_packet(tmp_path: Path):
    pkt = tmp_path / "hop.json"
    pkt.write_text("{}", encoding="utf-8")
    warnings = collect_run_preflight_warnings(
        tmp_path,
        packet_file=pkt,
    )
    joined = "\n".join(warnings)
    assert "vendor detect scrape skipped" in joined
    assert "LLM packet synthesis skipped" in joined


def test_collect_warnings_no_llm(monkeypatch, tmp_path: Path):
    _disable_llm(monkeypatch)
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "none")
    warnings = collect_run_preflight_warnings(tmp_path)
    joined = "\n".join(warnings)
    assert "LLM unavailable" in joined
    assert "client enrichment" in joined


def test_collect_warnings_skip_flags(tmp_path: Path):
    warnings = collect_run_preflight_warnings(
        tmp_path,
        skip_lockfile=True,
        skip_export_delta=True,
        skip_tests=True,
        skip_pr=True,
    )
    joined = "\n".join(warnings)
    assert "--skip-lockfile" in joined
    assert "--skip-export-delta" in joined
    assert "--skip-tests" in joined
    assert "--skip-pr" in joined


def test_print_preflight_shows_env_names_only(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    (tmp_path / ".env").write_text("OPENAI_KEY=secret-value\n", encoding="utf-8")
    loaded = load_consumer_env(tmp_path)
    assert "OPENAI_API_KEY" in loaded
    assert "secret-value" not in loaded

    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    print_run_preflight(
        console,
        warnings=["Tests skipped (--skip-tests)"],
        env_loaded=loaded,
    )
    out = buf.getvalue()
    assert "Loaded credentials from .env" in out
    assert "OPENAI_API_KEY" in out
    assert "secret-value" not in out
    assert "Warning:" in out


def test_agent_enrich_no_llm_adds_note(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")
    state = PackageClientState(package="openai", import_files=["app.py"])
    out = _agent_enrich(
        state,
        root=tmp_path,
        files=[tmp_path / "app.py"],
    )
    assert any(n.startswith("llm enrichment skipped") for n in out.notes)


def test_orchestrator_promotes_llm_skip_note(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")
    result = run_detect(
        tmp_path,
        skip_modules=True,
        skip_lockfile=True,
        scan_client=True,
        scan_packages=["openai"],
    )
    joined = "\n".join(result.warnings)
    assert "client state openai" in joined
    assert "llm enrichment skipped" in joined


def test_ensure_packet_file_load_warning(tmp_path: Path):
    pkt_path = tmp_path / "pkt.json"
    pkt_path.write_text(
        json.dumps(
            {
                "packet_id": "t",
                "package": "openai",
                "ecosystem": "pypi",
                "from_version": "0",
                "to_version": "1",
                "rules": [],
            }
        ),
        encoding="utf-8",
    )
    ensured = ensure_packet(
        tmp_path,
        [],
        package="openai",
        packet_path=pkt_path,
    )
    assert ensured.warnings
    assert "LLM packet synthesis/enrichment skipped" in ensured.warnings[0]


def test_describe_llm_status_none_provider(monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "none")
    status = describe_llm_status()
    assert not status.available
    assert "none" in (status.reason or "")
