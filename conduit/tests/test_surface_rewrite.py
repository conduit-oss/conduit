"""PR3: definite surface rewrites; possible stays untouched."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.surface.rewrite import apply_definite_surface_rewrites, replacement_chain
from conduit.surface.types import LexicalOnlyContract

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-validator-fixture"


def _dict_packet() -> dict:
    return {
        "packet_id": "pydantic-dict-definite",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.10.13",
        "to_version": "2.0.0",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "pydantic",
                "from_version": "1.10.13",
                "to_version": "2.0.0",
                "ecosystems": ["pip"],
                "reason": "pin",
            },
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "BaseModel.dict",
                "new_callee": "model_dump",
            },
        ],
    }


def _copy_fixture(dest: Path) -> Path:
    shutil.copytree(
        FIXTURE,
        dest,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", ".conduit"),
    )
    return dest


def test_replacement_chain_preserves_receiver():
    contract = LexicalOnlyContract(
        surface_id="t",
        old_callee="BaseModel.dict",
        new_callee="model_dump",
        export_path=("BaseModel", "dict"),
    )
    assert replacement_chain(contract, "self.dict") == "self.model_dump"
    assert replacement_chain(contract, "obj.dict") == "obj.model_dump"


def test_replacement_chain_bare_import():
    contract = LexicalOnlyContract(
        surface_id="t",
        old_callee="validator",
        new_callee="field_validator",
        export_path=("validator",),
    )
    assert replacement_chain(contract, "validator") == "field_validator"


def test_apply_rewrites_definite_self_dict(tmp_path: Path):
    tree = _copy_fixture(tmp_path / "fixture")
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    report = apply_definite_surface_rewrites(tree, _dict_packet(), dry_run=False)
    assert report.files_modified
    text = (tree / "src" / "model.py").read_text(encoding="utf-8")
    assert "self.model_dump()" in text
    assert "self.dict()" not in text
    # decorator was not a definite hit for this packet
    assert "@validator" in text


def test_apply_does_not_rewrite_member_only_possible(tmp_path: Path):
    tree = tmp_path / "loose"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "loose.py").write_text(
        textwrap.dedent(
            """\
            from pydantic import BaseModel

            def dump(payload):
                return payload.dict()
            """
        ),
        encoding="utf-8",
    )
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    report = apply_definite_surface_rewrites(tree, _dict_packet(), dry_run=False)
    assert report.files_modified == []
    assert "payload.dict()" in (tree / "src" / "loose.py").read_text(encoding="utf-8")


def test_definite_rewrite_respects_target_files_miss(tmp_path: Path):
    tree = tmp_path / "miss"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "app.py").write_text(
        "import widgets\n\nwidgets.legacy_fn()\n", encoding="utf-8"
    )
    (tree / "requirements.txt").write_text("widgets==2.0.0\n", encoding="utf-8")
    packet = {
        "packet_id": "widgets-miss",
        "package": "widgets",
        "ecosystem": "pypi",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["never_matches.py"],
                "old_callee": "legacy_fn",
                "new_callee": "modern_fn",
            }
        ],
    }
    report = apply_definite_surface_rewrites(tree, packet, dry_run=False)
    assert report.files_modified == []
    assert "widgets.legacy_fn()" in (tree / "src" / "app.py").read_text(encoding="utf-8")


def test_cli_apply_dict_packet_rewrites_self_dict(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "conduit.patcher.sync_env.sync_bumped_packages",
        lambda *args, **kwargs: [],
    )
    tree = _copy_fixture(tmp_path / "fixture")
    (tree / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    packet_path = tmp_path / "dict.json"
    packet_path.write_text(json.dumps(_dict_packet()), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app, ["apply", "--path", str(tree), "--packet", str(packet_path)]
    )
    assert result.exit_code == 0, result.output
    text = (tree / "src" / "model.py").read_text(encoding="utf-8")
    assert "self.model_dump()" in text
    assert "self.dict()" not in text
