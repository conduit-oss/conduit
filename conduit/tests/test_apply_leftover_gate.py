"""Apply leftover gate: rules-bearing residual old_* fails; pin-only stays exit 0."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.patcher.leftovers import (
    evaluate_apply_leftovers,
    packet_has_call_site_rules,
    scan_packet_leftovers,
)


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)


def _stub_sync(monkeypatch):
    monkeypatch.setattr(
        "conduit.patcher.sync_env.sync_bumped_packages",
        lambda *args, **kwargs: [],
    )


def _rules_bearing_packet(*, target_files: list[str]) -> dict:
    return {
        "packet_id": "widgets-1.0.0-2.0.0",
        "package": "widgets",
        "ecosystem": "pypi",
        "from_version": "1.0.0",
        "to_version": "2.0.0",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "widgets",
                "from_version": "1.0.0",
                "to_version": "2.0.0",
                "ecosystems": ["pip", "pyproject"],
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": target_files,
                "old_callee": "legacy_fn",
                "new_callee": "modern_fn",
            },
        ],
    }


def _pin_only_packet() -> dict:
    return {
        "packet_id": "widgets-1.0.0-2.0.0-pin",
        "package": "widgets",
        "ecosystem": "pypi",
        "from_version": "1.0.0",
        "to_version": "2.0.0",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "widgets",
                "from_version": "1.0.0",
                "to_version": "2.0.0",
                "ecosystems": ["pip", "pyproject"],
            }
        ],
    }


def _make_tree(root: Path, *, src: str, pin: str = "widgets==1.0.0") -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(src, encoding="utf-8")
    (root / "requirements.txt").write_text(pin + "\n", encoding="utf-8")
    return root


DIRTY_SRC = "import widgets\n\nwidgets.legacy_fn()\n"
CLEAN_SRC = "import widgets\n\nwidgets.modern_fn()\n"


def test_packet_has_call_site_rules_distinguishes_pin_only():
    assert packet_has_call_site_rules(_rules_bearing_packet(target_files=["*.py"]))
    assert not packet_has_call_site_rules(_pin_only_packet())


def test_apply_leftover_gate_fails_on_residual_old_callee(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    _stub_sync(monkeypatch)
    tree = _make_tree(tmp_path / "dirty", src=DIRTY_SRC)
    pkt = tmp_path / "packet.json"
    # target_files miss the consumer path so apply leaves old_callee in place.
    pkt.write_text(
        json.dumps(_rules_bearing_packet(target_files=["never_matches.py"])),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["apply", "--path", str(tree), "--packet", str(pkt)],
    )
    assert result.exit_code != 0, result.output
    assert "legacy_fn" in result.output
    assert "widgets.legacy_fn()" in (tree / "src" / "app.py").read_text(encoding="utf-8")


def test_apply_leftover_gate_passes_when_rewrite_clears(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    _stub_sync(monkeypatch)
    tree = _make_tree(tmp_path / "clean", src=DIRTY_SRC)
    pkt = tmp_path / "packet.json"
    pkt.write_text(
        json.dumps(_rules_bearing_packet(target_files=["*.py"])),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["apply", "--path", str(tree), "--packet", str(pkt)],
    )
    assert result.exit_code == 0, result.output
    text = (tree / "src" / "app.py").read_text(encoding="utf-8")
    assert "modern_fn" in text
    assert "legacy_fn" not in text


def test_apply_pin_only_exits_zero_with_message(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    _stub_sync(monkeypatch)
    tree = _make_tree(tmp_path / "pin", src=DIRTY_SRC)
    pkt = tmp_path / "packet.json"
    pkt.write_text(json.dumps(_pin_only_packet()), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["apply", "--path", str(tree), "--packet", str(pkt)],
    )
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "pin-only" in out or "no call-site rules" in out
    assert "widgets.legacy_fn()" in (tree / "src" / "app.py").read_text(encoding="utf-8")


def test_apply_dry_run_does_not_claim_leftover_pass(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    tree = _make_tree(tmp_path / "dry", src=DIRTY_SRC)
    pkt = tmp_path / "packet.json"
    pkt.write_text(
        json.dumps(_rules_bearing_packet(target_files=["never_matches.py"])),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["apply", "--path", str(tree), "--packet", str(pkt), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "apply complete" not in result.output.lower()
    assert "leftover call" not in result.output.lower()
    assert "widgets.legacy_fn()" in (tree / "src" / "app.py").read_text(encoding="utf-8")


def test_shared_leftover_helper_matches_watch_predicate(tmp_path: Path):
    tree = _make_tree(tmp_path / "shared", src=DIRTY_SRC, pin="widgets==2.0.0")
    packet = _rules_bearing_packet(target_files=["*.py"])
    watch_items = scan_packet_leftovers(tree, packet)
    verdict = evaluate_apply_leftovers(root=tree, packet=packet)
    assert watch_items
    assert any("legacy_fn" in item.callee for item in watch_items)
    assert verdict.exit_code != 0
    assert [item.display() for item in verdict.leftovers] == [
        item.display() for item in watch_items
    ]
