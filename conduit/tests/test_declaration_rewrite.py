"""AST_DECLARATION_REWRITE: import_member + nested class assign."""

from __future__ import annotations

from pathlib import Path

from conduit.packet.declaration_rules import (
    DeclarationRuleError,
    DecoratedDefConventionSpec,
    EnsureClassmethodSpec,
    ImportMemberSpec,
    Symbol,
    decode_declaration_rule,
    declaration_rules,
    declaration_stage,
)
from conduit.patcher.declarations.convention import (
    Conforms,
    DecoratedDefView,
    Refuse,
    classify_site,
)
from conduit.packet.validate import validate_packet
from conduit.patcher.ast_import_rewrite import rewrite_python_imports
from conduit.patcher.declarations import (
    rewrite_declarations,
    scan_declaration_residuals,
)
from conduit.patcher.engine import apply_packet
from conduit.patcher.py.imports import rewrite_import_member


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


def _convention_rule(decorator: str = "field_validator") -> dict:
    return {
        "type": "AST_DECLARATION_REWRITE",
        "target_files": ["*.py"],
        "operation": {
            "kind": "decorated_def_convention",
            "decorator": decorator,
            "context": None,
            "options": [],
            "unmappable_params": [],
            "unknown_params": "refuse",
            "unknown_options": "refuse",
        },
        "reason": f"v2 {decorator} takes (cls, value). Anything else is a located gap.",
    }


def _empty_spec(decorator: str = "field_validator") -> DecoratedDefConventionSpec:
    return DecoratedDefConventionSpec(decorator=decorator)


def _view(
    *,
    extra: tuple[str, ...] = (),
    leading: str | None = "cls",
    value: str | None = "v",
    name: str = "check_name",
    line: int = 7,
    has_star_args: bool = False,
    has_star_kwargs: bool = False,
    has_param_defaults: bool = False,
    is_bare_decorator: bool = False,
    decorator_kwargs: tuple[tuple[str, str], ...] = (),
    decorator_nonliteral_kwargs: tuple[str, ...] = (),
) -> DecoratedDefView:
    return DecoratedDefView(
        name=name,
        line=line,
        leading_param=leading,
        value_param=value,
        extra_params=extra,
        annotated_params=frozenset(),
        has_star_args=has_star_args,
        has_star_kwargs=has_star_kwargs,
        has_param_defaults=has_param_defaults,
        is_bare_decorator=is_bare_decorator,
        decorator_positional=1,
        decorator_kwargs=decorator_kwargs,
        decorator_nonliteral_kwargs=decorator_nonliteral_kwargs,
        body_reads=frozenset(),
        body_rebinds=frozenset(),
    )


def _ensure_classmethod_rule(decorator: str = "field_validator") -> dict:
    return {
        "type": "AST_DECLARATION_REWRITE",
        "target_files": ["*.py"],
        "operation": {
            "kind": "ensure_classmethod",
            "decorator": decorator,
        },
    }


def test_ensure_classmethod_on_field_validator(tmp_path: Path):
    src = '''\
from pydantic import BaseModel, field_validator

class User(BaseModel):
    name: str

    @field_validator("name")
    def check_name(cls, v):
        return v
'''
    path = tmp_path / "model.py"
    path.write_text(src, encoding="utf-8")
    rules = declaration_rules({"rules": [_ensure_classmethod_rule()]})
    pre = scan_declaration_residuals(path, src, rules, root=tmp_path)
    assert any(r.kind == "ensure_classmethod" for r in pre)
    result = rewrite_declarations(path, src, rules, root=tmp_path)
    assert result.applied >= 1
    assert "@classmethod" in result.content
    # decorator order: field_validator then classmethod
    assert result.content.index("@field_validator") < result.content.index("@classmethod")
    post = scan_declaration_residuals(path, result.content, rules, root=tmp_path)
    assert post == ()
    # idempotent
    again = rewrite_declarations(path, result.content, rules, root=tmp_path)
    assert again.applied == 0
    assert again.content == result.content


def test_schema_accepts_ensure_classmethod():
    packet = {
        "packet_id": "t",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.0",
        "to_version": "2.0",
        "rules": [_ensure_classmethod_rule()],
    }
    assert validate_packet(packet) == []


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


def test_classify_cls_v_conforms():
    verdict = classify_site(_view(), _empty_spec())
    assert isinstance(verdict, Conforms)


def test_classify_values_refuses_params():
    verdict = classify_site(_view(extra=("values",)), _empty_spec())
    assert isinstance(verdict, Refuse)
    assert verdict.gaps[0].kind == "decorated_def_params"
    assert "values" in verdict.gaps[0].old_shape
    assert "values" in verdict.gaps[0].reason


def test_classify_structural_opacity():
    cases = [
        _view(has_star_args=True),
        _view(has_star_kwargs=True),
        _view(has_param_defaults=True),
        _view(decorator_nonliteral_kwargs=("pre",)),
        _view(is_bare_decorator=True),
    ]
    for view in cases:
        verdict = classify_site(view, _empty_spec())
        assert isinstance(verdict, Refuse)
        assert any("opaque" in gap.reason for gap in verdict.gaps)


def test_declaration_stage_convention_is_late():
    assert declaration_stage(_empty_spec()) == "late"
    assert declaration_stage(EnsureClassmethodSpec("field_validator")) == "late"
    assert (
        declaration_stage(
            ImportMemberSpec(
                source=Symbol("pydantic", "validator"),
                target=Symbol("pydantic", "field_validator"),
            )
        )
        == "early"
    )


def test_decode_rejects_absorbs_without_context():
    raw = _convention_rule()
    raw["operation"]["absorbs"] = {"values": "data"}
    try:
        decode_declaration_rule(raw)
        assert False, "expected absorbs-without-context to fail"
    except DeclarationRuleError as exc:
        assert "absorbs" in str(exc)


def test_schema_accepts_decorated_def_convention():
    packet = {
        "packet_id": "t",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.0",
        "to_version": "2.0",
        "rules": [_convention_rule(), _convention_rule("model_validator")],
    }
    assert validate_packet(packet) == []


def test_cls_v_scan_clean_and_no_info_insert(tmp_path: Path):
    src = '''\
from pydantic import BaseModel, field_validator

class User(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def check_name(cls, v):
        return v
'''
    path = tmp_path / "model.py"
    path.write_text(src, encoding="utf-8")
    rules = declaration_rules({"rules": [_convention_rule()]})
    result = rewrite_declarations(path, src, rules, root=tmp_path)
    assert result.applied == 0
    assert result.residuals == ()
    assert "def check_name(cls, v):" in result.content
    assert "info" not in result.content
    assert scan_declaration_residuals(path, result.content, rules, root=tmp_path) == ()


def test_values_residual_has_line_and_kind(tmp_path: Path):
    src = '''\
from pydantic import BaseModel, field_validator

class Order(BaseModel):
    total: int

    @field_validator("total")
    @classmethod
    def check_total(cls, v, values):
        return v
'''
    path = tmp_path / "model.py"
    path.write_text(src, encoding="utf-8")
    rules = declaration_rules({"rules": [_convention_rule()]})
    pre = scan_declaration_residuals(path, src, rules, root=tmp_path)
    assert len(pre) == 1
    hit = pre[0]
    assert hit.kind == "decorated_def_params"
    assert "values" in hit.old_shape
    assert "values" in hit.reason
    assert hit.line >= 1
    result = rewrite_declarations(path, src, rules, root=tmp_path)
    assert result.applied == 0
    assert "def check_total(cls, v, values):" in result.content
    assert any(r.kind == "decorated_def_params" for r in result.residuals)


def test_convention_runs_late_after_classmethod(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    src = '''\
from pydantic import BaseModel, validator

class Order(BaseModel):
    total: int

    @validator("total")
    def check_total(cls, v, values):
        return v
'''
    path = tmp_path / "m.py"
    path.write_text(src, encoding="utf-8")
    packet = {
        "packet_id": "t",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.10.13",
        "to_version": "2.0.0",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "validator",
                "new_callee": "field_validator",
            },
            _ensure_classmethod_rule(),
            _convention_rule(),
        ],
    }
    apply_packet(tmp_path, packet, require_context=False)
    text = path.read_text(encoding="utf-8")
    assert "@field_validator" in text
    assert "@classmethod" in text
    assert text.index("@field_validator") < text.index("@classmethod")
    assert "def check_total(cls, v, values):" in text
    assert "info" not in text
    decl = declaration_rules(packet)
    hits = scan_declaration_residuals(path, text, decl, root=tmp_path)
    assert any(r.kind == "decorated_def_params" and "values" in r.reason for r in hits)
