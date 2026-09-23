"""Declared synthetic convention shapes: apply / Watch completeness bar."""

from __future__ import annotations

import json
from pathlib import Path

from conduit.packet.declaration_rules import declaration_rules
from conduit.patcher.declarations.apply import (
    rewrite_declarations,
    scan_declaration_residuals,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-convention-fixture" / "src"
RECIPE = REPO / "examples" / "reshape-recipes" / "pydantic-1.10.13-2.0.0.json"


def _convention_rules():
    recipe = json.loads(RECIPE.read_text(encoding="utf-8"))
    return declaration_rules(
        {
            "rules": [
                r
                for r in recipe["rules"]
                if r.get("type") == "AST_DECLARATION_REWRITE"
                and (r.get("operation") or {}).get("kind") == "decorated_def_convention"
            ]
        }
    )


def test_bare_cls_v_conforms_no_info_insert():
    path = FIXTURE / "bare_cls_v.py"
    src = path.read_text(encoding="utf-8")
    rules = _convention_rules()
    result = rewrite_declarations(path, src, rules, root=FIXTURE)
    assert result.applied == 0
    assert result.residuals == ()
    assert "def check_name(cls, v):" in result.content
    assert "info" not in result.content
    assert scan_declaration_residuals(path, result.content, rules, root=FIXTURE) == ()


def test_pre_true_applies_clean():
    path = FIXTURE / "pre_true.py"
    src = path.read_text(encoding="utf-8")
    rules = _convention_rules()
    result = rewrite_declarations(path, src, rules, root=FIXTURE)
    assert result.applied >= 1
    assert result.residuals == ()
    assert '@field_validator("name", pre=True)' not in result.content
    assert "mode" in result.content and "before" in result.content
    assert scan_declaration_residuals(path, result.content, rules, root=FIXTURE) == ()


def test_trailing_values_applies_clean():
    path = FIXTURE / "trailing_values.py"
    src = path.read_text(encoding="utf-8")
    rules = _convention_rules()
    result = rewrite_declarations(path, src, rules, root=FIXTURE)
    assert result.applied >= 1
    assert result.residuals == ()
    assert "info.data" in result.content
    assert "ValidationInfo" in result.content
    sig_and_body = result.content.split("def check_total", 1)[1]
    assert "values" not in sig_and_body
    assert scan_declaration_residuals(path, result.content, rules, root=FIXTURE) == ()


def test_each_item_stays_dirty():
    path = FIXTURE / "each_item.py"
    src = path.read_text(encoding="utf-8")
    rules = _convention_rules()
    result = rewrite_declarations(path, src, rules, root=FIXTURE)
    assert result.applied == 0
    assert '@field_validator("tags", each_item=True)' in result.content
    assert any(r.kind == "decorated_def_options" for r in result.residuals)
    assert scan_declaration_residuals(path, src, rules, root=FIXTURE)
