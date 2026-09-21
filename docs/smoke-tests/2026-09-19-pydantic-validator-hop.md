# Smoke: pydantic validator hop (stage-2 reshape)

Offline, no LLM keys. Proves CLI `conduit watch` → `conduit apply` → `conduit watch`
exits 1 → 0 → 0 for a frozen gold-shape hop on a tiny fixture: `validator` →
`field_validator` + `@classmethod`, `BaseModel.dict` → `model_dump`, inner `Config` →
`model_config`. Does not claim a mergeable pydantic v2 upgrade of a real consumer.

## Inputs

| Item | Path |
|------|------|
| Fixture | `examples/pydantic-validator-fixture/` (`pydantic==1.10.13`, `@validator("name")`, `self.dict()`, nested `Config`) |
| Packet | `examples/sample-packet/pydantic-validator-hop.json` |
| Packet SHA-256 | `46111294e8e73ddf449279de81f536bc4162b6b6c3f69dd25eb297d7690f5514` |
| Receipt | `docs/smoke-tests/check-remint-receipt.py --packet examples/sample-packet/pydantic-validator-hop.json` |
| Unit | `conduit/tests/test_pydantic_validator_smoke.py` (CliRunner) |

Packet rules: `DEPENDENCY_BUMP` to `2.0.0`; `AST_DECLARATION_REWRITE`
`import_member` + `inner_class_to_assignment` + `ensure_classmethod`;
`AST_CALL_REWRITE` for bare `validator` and `BaseModel.dict`.

Structured `side_effects` hold remaining signature/`mode=` gap (`gap_kind=signature`).

## Gold obligations (typed receipt)

| Shape | Mode |
|-------|------|
| dict (`AST_CALL_REWRITE` leaf) | mechanical |
| validator (CALL + `import_member`) | mechanical |
| Config (`inner_class_to_assignment`) | mechanical |
| `@classmethod` on `field_validator` (`ensure_classmethod`) | mechanical |

## Measured run

Seed the fixture pin at `pydantic==2.0.0` so the first Watch is `bump_dirty`, not
`pre_bump`. Then apply and Watch again on the same tree.

| Step | Result |
|------|--------|
| CLI Watch at `to_version` with seeded leftover | exit 1 (`validator` leftover) |
| CLI apply | exit 0; `@field_validator` + `@classmethod`; `self.model_dump()`; `model_config = ConfigDict(...)`; pin `2.0.0` |
| Second CLI apply | Model and requirements bytes unchanged |
| CLI Watch post | exit 0; leftovers `[]` |
| Residue `.dict(` | 0 |
| Residue `class Config` | 0 |

## Dual verdict

| Gate | Verdict |
|------|---------|
| Gate validity (Watch red → green; gold shapes mechanical) | PASS |
| Migration completeness (full pydantic v2 / mergeable consumer) | FAIL |

## Pytest

```text
python -m pytest -q conduit/tests/test_pydantic_validator_smoke.py
..                                                                       [100%]
2 passed
```
