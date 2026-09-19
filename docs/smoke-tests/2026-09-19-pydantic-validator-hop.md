# Smoke: pydantic validator hop (2026-09-19)

Offline, no LLM keys. Proves Watch dirty → apply → Watch clean for a frozen
`validator` → `field_validator` hop on a tiny fixture. Does not claim a mergeable
pydantic v2 upgrade of pydavinci or any other real consumer.

## Inputs

| Item | Path |
|------|------|
| Fixture | `examples/pydantic-validator-fixture/` (`pydantic==1.10.13`, `@validator("name")`) |
| Packet | `examples/sample-packet/pydantic-validator-hop.json` |
| Unit | `conduit/tests/test_pydantic_validator_smoke.py` |

Packet rules: `DEPENDENCY_BUMP` to `2.0.0`, `AST_IMPORT_REWRITE` on the import-clause
fragment `BaseModel, validator` → `BaseModel, field_validator`, then
`AST_CALL_REWRITE` `validator` → `field_validator`. Import rewrite uses the clause
fragment on purpose. A bare `validator` string fallback would re-prefix
`field_validator` on a second apply.

`side_effects` name uncovered work: `.dict()`, inner `Config`, and
signature / `classmethod` reshape.

## Inventory

| Bucket | Items |
|--------|-------|
| Watch-visible | Pin at `to_version` with leftover `old_callee` `validator` |
| Apply-only | Import-clause rewrite (`AST_IMPORT_REWRITE`); pin bump |
| Out-of-engine | `.dict()`, `class Config` / `orm_mode`, `@classmethod` / signature / `mode=` |

## Measured run (head of this PR)

| Step | Result |
|------|--------|
| Watch at `to_version` with seeded leftover | `bump_dirty`, exit 1, leftover callee `validator` |
| Apply | `@field_validator("name")`, pin `pydantic==2.0.0`, no `field_field_validator` |
| Second apply | Model and requirements bytes unchanged (report may still list `requirements.txt` for `DEPENDENCY_BUMP`) |
| Watch post | `clean`, exit 0 |
| Residue `.dict(` count | 1 |
| Residue `class Config` count | 1 |
| Apply + Watch wall time | 0.03 s (under 60 s budget) |

Commands used no LLM env vars.

## Dual verdict

| Gate | Verdict |
|------|---------|
| Gate validity (Watch red → green for this hop, no double prefix) | PASS |
| Migration completeness (full pydantic v2 / mergeable consumer) | FAIL |

Full v2 still needs the `side_effects` work and more. This smoke only proves the
Watch-visible hop on a fixture that avoids pydavinci’s `settings.validator`
module-name smash.

## Pytest

```text
python -m pytest -q conduit/tests/test_pydantic_validator_smoke.py
..                                                                       [100%]
2 passed
```
