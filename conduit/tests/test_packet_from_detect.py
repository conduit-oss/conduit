"""Publisher snapshot packets from detect."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.detect.models import ChangeSignal
from conduit.detect.modules.openai.workers.sdk_release import SDKReleaseWorker
from conduit.main import app
from conduit.packet.from_detect import (
    SNAPSHOT_FLOOR,
    build_snapshot_packet,
    snapshot_packet_id,
    snapshot_targets_from_signals,
    write_snapshots,
)
from conduit.packet.validate import validate_packet


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)


def _bump_signal(*, eco: str, to_version: str, package: str = "openai") -> ChangeSignal:
    ecosystems = ["npm"] if eco == "npm" else ["pip", "pyproject"]
    return ChangeSignal(
        source="module:openai",
        package=package,
        change_type="SDK_MAJOR_BUMP",
        from_version="0",
        to_version=to_version,
        ecosystem=eco,
        suggested_rules=[
            {
                "type": "DEPENDENCY_BUMP",
                "package": package,
                "from_version": "0",
                "to_version": to_version,
                "ecosystems": ecosystems,
            }
        ],
    )


def _model_signal() -> ChangeSignal:
    return ChangeSignal(
        source="module:openai",
        package="openai",
        change_type="MODEL_DEPRECATION",
        affected_pattern="gpt-4-0613",
        replacement_pattern="gpt-4o",
        suggested_rules=[
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            }
        ],
    )


def test_sdk_catalog_latest_emits_python_and_node():
    worker = SDKReleaseWorker()
    signals = worker.run(demo=True, client_state=None, catalog_latest=True)
    by_eco = {
        tuple(s.extra.get("ecosystems") or []): s.extra.get("to_version") for s in signals
    }
    assert by_eco[("pip", "pyproject")] == "2.0.0"
    assert by_eco[("npm",)] == "5.0.0"


def test_sdk_still_skips_without_pin_when_not_catalog():
    worker = SDKReleaseWorker()
    assert worker.run(demo=True, client_state=None, catalog_latest=False) == []
    assert worker.last_skip_reason == "no_installed_version"


def test_snapshot_targets_split_ecosystems():
    targets = snapshot_targets_from_signals(
        [_bump_signal(eco="pypi", to_version="2.0.0"), _bump_signal(eco="npm", to_version="5.0.0")]
    )
    ecos = {t.ecosystem: t.to_version for t in targets}
    assert ecos["pypi"] == "2.0.0"
    assert ecos["npm"] == "5.0.0"


def test_first_snapshot_from_floor(tmp_path: Path):
    writes = write_snapshots(
        [_bump_signal(eco="pypi", to_version="2.0.0"), _model_signal()],
        out_dir=tmp_path,
        package="openai",
        ecosystem="pypi",
    )
    assert len(writes) == 1
    assert not writes[0].skipped
    pkt = writes[0].packet
    assert pkt["packet_id"] == "openai-pypi-2.0.0"
    assert pkt["from_version"] == SNAPSHOT_FLOOR
    assert pkt["to_version"] == "2.0.0"
    assert pkt["ecosystem"] == "pypi"
    assert validate_packet(pkt) == []
    bump = next(r for r in pkt["rules"] if r["type"] == "DEPENDENCY_BUMP")
    assert bump["to_version"] == "2.0.0"
    assert bump["ecosystems"] == ["pip", "pyproject"]
    assert any(r.get("match") == "gpt-4-0613" for r in pkt["rules"])
    assert not any(
        r.get("type") == "AST_CALL_REWRITE"
        and r.get("old_callee") == "ChatCompletion.create"
        for r in pkt["rules"]
    )


def test_snapshot_includes_callee_migration_from_endpoint_signals(tmp_path: Path):
    endpoint = ChangeSignal(
        source="module:openai",
        package="openai",
        change_type="API_BREAKING",
        affected_pattern="/v1/completions",
        replacement_pattern="/v1/chat/completions",
        source_url="https://platform.openai.com/docs/deprecations",
    )
    from conduit.detect.modules.openai.sdk_callee_migration import apply_sdk_callee_migration

    scoped, _ = apply_sdk_callee_migration([endpoint])
    pkt = build_snapshot_packet(
        scoped + [_bump_signal(eco="pypi", to_version="2.0.0")],
        package="openai",
        ecosystem="pypi",
        from_version="0",
        to_version="2.0.0",
    )
    assert any(
        r.get("type") == "AST_CALL_REWRITE"
        and r.get("old_callee") == "Completion.create"
        for r in pkt["rules"]
    )


def test_second_hop_deltas_and_skips_same_latest(tmp_path: Path):
    first = write_snapshots(
        [_bump_signal(eco="pypi", to_version="1.0.0"), _model_signal()],
        out_dir=tmp_path,
        package="openai",
        ecosystem="pypi",
    )
    assert first[0].packet["to_version"] == "1.0.0"

    second = write_snapshots(
        [_bump_signal(eco="pypi", to_version="2.0.0"), _model_signal()],
        out_dir=tmp_path,
        package="openai",
        ecosystem="pypi",
    )
    pkt = second[0].packet
    assert pkt["from_version"] == "1.0.0"
    assert pkt["to_version"] == "2.0.0"
    assert pkt["packet_id"] == snapshot_packet_id("openai", "pypi", "2.0.0")
    assert not any(r.get("match") == "gpt-4-0613" for r in pkt["rules"])
    bump = next(r for r in pkt["rules"] if r["type"] == "DEPENDENCY_BUMP")
    assert bump["from_version"] == "1.0.0"
    assert bump["to_version"] == "2.0.0"

    again = write_snapshots(
        [_bump_signal(eco="pypi", to_version="2.0.0"), _model_signal()],
        out_dir=tmp_path,
        package="openai",
        ecosystem="pypi",
    )
    assert again[0].skipped
    text = (tmp_path / "openai-pypi-2.0.0.json").read_text(encoding="utf-8")
    assert json.loads(text)["from_version"] == "1.0.0"


def test_npm_bump_stays_on_npm_packet(tmp_path: Path):
    writes = write_snapshots(
        [
            _bump_signal(eco="pypi", to_version="2.0.0"),
            _bump_signal(eco="npm", to_version="5.0.0"),
        ],
        out_dir=tmp_path,
        package="openai",
    )
    by_eco = {w.packet["ecosystem"]: w.packet for w in writes}
    pypi_bump = next(r for r in by_eco["pypi"]["rules"] if r["type"] == "DEPENDENCY_BUMP")
    npm_bump = next(r for r in by_eco["npm"]["rules"] if r["type"] == "DEPENDENCY_BUMP")
    assert pypi_bump["ecosystems"] == ["pip", "pyproject"]
    assert npm_bump["ecosystems"] == ["npm"]
    assert pypi_bump["to_version"] == "2.0.0"
    assert npm_bump["to_version"] == "5.0.0"


def test_build_snapshot_packet_validates():
    pkt = build_snapshot_packet(
        [_bump_signal(eco="pypi", to_version="2.0.0")],
        package="openai",
        ecosystem="pypi",
        from_version="0",
        to_version="2.0.0",
    )
    assert validate_packet(pkt) == []


def test_cli_from_detect_demo(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    _disable_llm(monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "packet",
            "from-detect",
            "--module",
            "openai",
            "--demo",
            "--out-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    pypi = tmp_path / "openai-pypi-2.0.0.json"
    npm = tmp_path / "openai-npm-5.0.0.json"
    assert pypi.is_file(), result.output
    assert npm.is_file(), result.output
    pypi_pkt = json.loads(pypi.read_text(encoding="utf-8"))
    npm_pkt = json.loads(npm.read_text(encoding="utf-8"))
    assert pypi_pkt["from_version"] == "0"
    assert pypi_pkt["to_version"] == "2.0.0"
    assert npm_pkt["to_version"] == "5.0.0"
    assert validate_packet(pypi_pkt) == []
    assert not (tmp_path / ".conduit" / "source-packets").exists()

    again = CliRunner().invoke(
        app,
        [
            "packet",
            "from-detect",
            "--module",
            "openai",
            "--demo",
            "--out-dir",
            str(tmp_path),
        ],
    )
    assert again.exit_code == 0, again.output
    assert "already have" in again.output

    only_pypi = CliRunner().invoke(
        app,
        [
            "packet",
            "from-detect",
            "--module",
            "openai",
            "--demo",
            "--ecosystem",
            "pypi",
            "--out-dir",
            str(tmp_path / "one"),
        ],
    )
    assert only_pypi.exit_code == 0, only_pypi.output
    names = {p.name for p in (tmp_path / "one").glob("*.json")}
    assert names == {"openai-pypi-2.0.0.json"}


def test_cli_unknown_module():
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app, ["packet", "from-detect", "--module", "not-a-module"]
    )
    assert result.exit_code == 2
    assert "Unknown detect module" in result.output
