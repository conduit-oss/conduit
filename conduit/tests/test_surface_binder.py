"""Surface binder: LexicalOnly compile + fixture binding for live-mint miss."""

from __future__ import annotations

from pathlib import Path

from conduit.surface import (
    Confidence,
    LexicalOnlyContract,
    MatchEvidence,
    Spelling,
    VerdictStatus,
    bind,
    contracts_from_packet,
    evaluate_binding,
    index_python,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "pydantic-validator-fixture"


def _dict_packet() -> dict:
    return {
        "packet_id": "pydantic-dict-miss",
        "package": "pydantic",
        "ecosystem": "pypi",
        "from_version": "1.10.13",
        "to_version": "2.0.0",
        "rules": [
            {
                "type": "AST_CALL_REWRITE",
                "target_files": ["*.py"],
                "old_callee": "BaseModel.dict",
                "new_callee": "model_dump",
                "reason": "live mint miss shape",
            }
        ],
    }


def _validator_packet() -> dict:
    return {
        "packet_id": "pydantic-validator-hop",
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
            }
        ],
    }


def test_contracts_from_packet_lexical_compile():
    contracts = contracts_from_packet(_dict_packet())
    assert len(contracts) == 1
    contract = contracts[0]
    assert isinstance(contract, LexicalOnlyContract)
    assert contract.old_callee == "BaseModel.dict"
    assert contract.new_callee == "model_dump"
    assert contract.export_path == ("BaseModel", "dict")
    assert contract.proof_eligible is False


def test_basemodel_dict_packet_is_not_complete():
    root = FIXTURE
    files = [root / "src" / "model.py"]
    packet = _dict_packet()
    contracts = contracts_from_packet(packet)
    index = index_python(root, files)
    observations = bind(contracts, index, "pydantic")
    verdict = evaluate_binding(contracts, observations)

    assert verdict.status != VerdictStatus.COMPLETE
    assert any(
        o.chain.endswith(".dict") or o.chain in {"obj.dict", "self.dict"}
        for o in observations
    )


def test_validator_packet_observes_imported_decorator():
    root = FIXTURE
    files = [root / "src" / "model.py"]
    packet = _validator_packet()
    contracts = contracts_from_packet(packet)
    index = index_python(root, files)
    observations = bind(contracts, index, "pydantic")

    hits = [o for o in observations if o.chain == "validator"]
    assert hits, "expected @validator observation"
    assert any(
        o.confidence == Confidence.DEFINITE
        and o.spelling == Spelling.IMPORTED
        and o.evidence == MatchEvidence.IMPORT_ALIAS
        for o in hits
    )
