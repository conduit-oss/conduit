"""Mint attaches proof-eligible surface floor to AST_CALL_REWRITE rules."""

from __future__ import annotations

from pathlib import Path

from conduit.packet.synthesize import normalize_llm_rules
from conduit.packet.validate import validate_packet
from conduit.surface import (
    SurfaceContract,
    VerdictStatus,
    contracts_from_packet,
    derive_surface_floor,
    enrich_minted_rules,
    evaluate_packet_binding,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-validator-fixture"


def test_derive_floor_dotted_includes_receiver_member():
    floor = derive_surface_floor("BaseModel.dict", "model_dump")
    assert floor is not None
    assert floor["export_path"] == ["BaseModel", "dict"]
    assert "receiver_member" in floor["spellings"]
    assert "imported" in floor["spellings"]
    assert "call" in floor["use_kinds"]


def test_derive_floor_bare_includes_imported_and_decorator():
    floor = derive_surface_floor("validator", "field_validator")
    assert floor is not None
    assert floor["export_path"] == ["validator"]
    assert "imported" in floor["spellings"]
    assert "decorator" in floor["use_kinds"]


def test_normalize_llm_rules_attaches_surface_and_validates():
    rules = normalize_llm_rules(
        [
            {
                "type": "AST_CALL_REWRITE",
                "old": "BaseModel.dict",
                "new": "model_dump",
                "reason": "rename",
            },
            {
                "type": "DEPENDENCY_BUMP",
                "package": "pydantic",
                "from_version": "*",
                "to_version": "2.0.0",
                "ecosystems": ["pip"],
                "reason": "pin",
            },
        ]
    )
    call = next(r for r in rules if r["type"] == "AST_CALL_REWRITE")
    assert call["old_callee"] == "BaseModel.dict"
    assert isinstance(call.get("surface"), dict)
    assert call["surface"]["export_path"] == ["BaseModel", "dict"]
    packet = {
        "packet_id": "pydantic-surface-floor-test",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "*",
        "to_version": "2.0.0",
        "rules": rules,
    }
    assert validate_packet(packet) == []


def test_enrich_preserves_authored_valid_surface():
    rules = enrich_minted_rules(
        [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "BaseModel.dict",
                "new_callee": "model_dump",
                "surface": {
                    "export_path": ["BaseModel", "dict"],
                    "spellings": ["imported", "receiver_member"],
                    "use_kinds": ["call"],
                },
            }
        ]
    )
    assert rules[0]["surface"]["spellings"] == ["imported", "receiver_member"]


def test_enrich_replaces_invalid_surface_with_floor():
    rules = enrich_minted_rules(
        [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "BaseModel.dict",
                "new_callee": "model_dump",
                "surface": {"spellings": ["qualified"]},  # missing proof spellings
            }
        ]
    )
    assert "receiver_member" in rules[0]["surface"]["spellings"]


def test_minted_dict_packet_not_unverified_on_fixture():
    packet = {
        "packet_id": "pydantic-dict-surface",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.10.13",
        "to_version": "2.0.0",
        "rules": normalize_llm_rules(
            [
                {
                    "type": "AST_CALL_REWRITE",
                    "old_callee": "BaseModel.dict",
                    "new_callee": "model_dump",
                    "reason": "docs",
                }
            ]
        ),
    }
    contracts = contracts_from_packet(packet)
    assert len(contracts) == 1
    assert isinstance(contracts[0], SurfaceContract)
    assert contracts[0].proof_eligible is True
    verdict = evaluate_packet_binding(FIXTURE, packet)
    assert verdict.status != VerdictStatus.UNVERIFIED
    assert verdict.status == VerdictStatus.INCOMPLETE
    assert any(o.chain.endswith(".dict") for o in verdict.observations)
