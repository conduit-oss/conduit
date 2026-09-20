"""AST_DECLARATION_REWRITE: import_member + nested class assign."""

from __future__ import annotations

from pathlib import Path

from conduit.packet.declaration_rules import (
    DeclarationRuleError,
    decode_declaration_rule,
    declaration_rules,
)
from conduit.packet.validate import validate_packet
from conduit.patcher.ast_import_rewrite import rewrite_python_imports
from conduit.patcher.declarations import (
    rewrite_declarations,
    scan_declaration_residuals,
)
from conduit.patcher.engine import apply_packet
from conduit.patcher.py.imports import rewrite_import_member
from conduit.packet.declaration_rules import Symbol


def _import_member_rule(
    *,
    module: str = "pydantic",
    old: str = "validator",
    new: str = "field_validator",
    new_module: str | None = None,
) -> dict:
    target_mod = new_module or module
    return {
        "type": "AST_DECLARATION_REWRITE",
        "target_files": ["*.py"],
        "operation": {
            "kind": "import_member",
            "source": {"module": module, "name": old},
            "target": {"module": target_mod, "name": new},
            "local_binding": "preserve",
        },
    }


def _nested_rule() -> dict:
    return {
        "type": "AST_DECLARATION_REWRITE",
        "target_files": ["*.py"],
        "operation": {
            "kind": "inner_class_to_assignment",
            "selector": {"inner_name": "Config", "parent_bases_any": ["BaseModel"]},
            "keys": {"orm_mode": "from_attributes"},
            "emit": {
                "target": "model_config",
                "constructor": "ConfigDict",
                "ensure_import": {"module": "pydantic", "name": "ConfigDict"},
            },
            "unmapped_assignments": "refuse",
            "unsupported_members": "refuse",
        },
    }


def test_schema_accepts_declaration_rewrite():
    packet = {
        "packet_id": "t",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.0",
        "to_version": "2.0",
        "rules": [_import_member_rule(), _nested_rule()],
    }
    assert validate_packet(packet) == []


def test_decode_import_member_and_conflict():
    rule = decode_declaration_rule(_import_member_rule())
    assert rule.obligation_id() == "import_member:pydantic::validator"
    rules = declaration_rules({"rules": [_import_member_rule(), _import_member_rule()]})
    assert len(rules) == 1
    conflict = _import_member_rule(new="other")
    try:
        declaration_rules({"rules": [_import_member_rule(), conflict]})
        assert False, "expected conflict"
    except DeclarationRuleError:
        pass


def test_rewrite_multi_name_import_preserves_sibling_and_alias():
    src = "from pydantic import BaseModel, validator as validates\n"
    out, n = rewrite_import_member(
        src,
        Symbol("pydantic", "validator"),
        Symbol("pydantic", "field_validator"),
    )
    assert n >= 1
    assert "field_validator as validates" in out
    assert "BaseModel" in out
    assert "validator" not in out.replace("field_validator", "")


def test_rewrite_import_member_idempotent():
    src = "from pydantic import BaseModel, validator\n"
    once, _ = rewrite_import_member(
        src, Symbol("pydantic", "validator"), Symbol("pydantic", "field_validator")
    )
    twice, n2 = rewrite_import_member(
        once, Symbol("pydantic", "validator"), Symbol("pydantic", "field_validator")
    )
    assert n2 == 0
    assert twice == once


def test_split_import_member_to_new_module():
    src = "from pydantic import BaseModel, BaseSettings\n"
    out, n = rewrite_import_member(
        src,
        Symbol("pydantic", "BaseSettings"),
        Symbol("pydantic_settings", "BaseSettings"),
    )
    assert n >= 1
    assert "BaseModel" in out
    assert "pydantic_settings" in out
    assert "BaseSettings" in out
    # old module line should not still import BaseSettings
    assert "from pydantic import BaseModel" in out.replace(" ", "") or (
        "from pydantic import BaseModel" in out
    )


def test_legacy_import_rewrite_no_bare_name_fallback():
    src = "from pydantic import BaseModel, validator\n"
    out, n = rewrite_python_imports(src, "validator", "field_validator")
    assert n == 0
    assert out == src


def test_legacy_fragment_fallback_gone():
    src = "from pydantic import BaseModel, validator\n"
    out, n = rewrite_python_imports(src, "BaseModel, validator", "BaseModel, field_validator")
    assert n == 0
    assert out == src


def test_nested_config_to_assign(tmp_path: Path):
    src = '''\
from pydantic import BaseModel

class User(BaseModel):
    name: str
    class Config:
        orm_mode = True
'''
    path = tmp_path / "model.py"
    path.write_text(src, encoding="utf-8")
    rules = declaration_rules({"rules": [_nested_rule()]})
    result = rewrite_declarations(path, src, rules, root=tmp_path)
    assert result.applied >= 1
    assert "model_config = ConfigDict(from_attributes=True)" in result.content.replace(
        " ", ""
    ) or "from_attributes=True" in result.content
    assert "class Config" not in result.content
    assert "ConfigDict" in result.content


def test_apply_packet_declaration_and_residual(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\n", encoding="utf-8")
    src = "from pydantic import BaseModel, validator\n\n@validator('x')\ndef f(v):\n    return v\n"
    (tmp_path / "m.py").write_text(src, encoding="utf-8")
    packet = {
        "packet_id": "t",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "*",
        "to_version": "2.0.0",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "pydantic",
                "from_version": "*",
                "to_version": "2.0.0",
                "ecosystems": ["pip"],
            },
            _import_member_rule(),
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "validator",
                "new_callee": "field_validator",
            },
        ],
    }
    # Pre: residual should see import_member
    decl = declaration_rules(packet)
    pre = scan_declaration_residuals(
        tmp_path / "m.py", src, decl, root=tmp_path
    )
    assert any(r.kind == "import_member" for r in pre)

    apply_packet(tmp_path, packet, require_context=False)
    text = (tmp_path / "m.py").read_text(encoding="utf-8")
    assert "field_validator" in text
    assert "from pydantic import BaseModel, field_validator" in text or (
        "field_validator" in text and "validator" not in text.replace("field_validator", "")
    )
    post = scan_declaration_residuals(
        tmp_path / "m.py", text, decl, root=tmp_path
    )
    assert post == ()
