# Smoke: pydantic validator hop (updated for remint-trust cook)

Offline, no LLM keys. Proves CLI `conduit watch` → `conduit apply` → `conduit watch`
exits 1 → 0 → 0 for a frozen gold-shape hop on a tiny fixture: `validator` →
`field_validator`, `BaseModel.dict` → `model_dump`, inner `Config` → `model_config`.
Does not claim a mergeable pydantic v2 upgrade of a real consumer.

## Inputs

| Item | Path |
|------|------|
| Fixture | `examples/pydantic-validator-fixture/` (`pydantic==1.10.13`, `@validator("name")`, `self.dict()`, nested `Config`) |
| Packet | `examples/sample-packet/pydantic-validator-hop.json` |
| Packet SHA-256 | `7d64c3ff437b01479f6fdb65d937527ec2667683e524af3bead17356eda3c3a6` |
| Receipt | `docs/smoke-tests/check-remint-receipt.py --packet examples/sample-packet/pydantic-validator-hop.json` |
| Unit | `conduit/tests/test_pydantic_validator_smoke.py` (CliRunner) |

Packet rules: `DEPENDENCY_BUMP` to `2.0.0`; `AST_DECLARATION_REWRITE`
`import_member` + `inner_class_to_assignment`; `AST_CALL_REWRITE` for bare
`validator` and `BaseModel.dict`. Clause-fragment `AST_IMPORT_REWRITE` is refused
at normalize; companions come from cook.

`side_effects` name uncovered reshape work (`@classmethod` / signature / `mode=`).

## Gold obligations (typed receipt)

| Shape | Mode |
|-------|------|
| dict (`AST_CALL_REWRITE` leaf) | mechanical |
| validator (CALL + `import_member`) | mechanical |
| Config (`inner_class_to_assignment`) | mechanical |

## Measured run

Seed the fixture pin at `pydantic==2.0.0` so the first Watch is `bump_dirty`, not
`pre_bump`. Then apply and Watch again on the same tree.

| Step | Result |
|------|--------|
| CLI Watch at `to_version` with seeded leftover | exit 1 (`validator` leftover) |
| CLI apply | exit 0; `@field_validator`; `self.model_dump()`; `model_config = ConfigDict(...)`; pin `2.0.0` |
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
