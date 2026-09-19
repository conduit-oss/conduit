# Smoke: pydantic validator hop (2026-09-19)

Offline, no LLM keys. Proves CLI `conduit watch` → `conduit apply` → `conduit watch`
exits 1 → 0 → 0 for a frozen `validator` → `field_validator` hop on a tiny fixture.
Does not claim a mergeable pydantic v2 upgrade of pydavinci or any other real consumer.

## Inputs

| Item | Path |
|------|------|
| Fixture | `examples/pydantic-validator-fixture/` (`pydantic==1.10.13`, `@validator("name")`) |
| Packet | `examples/sample-packet/pydantic-validator-hop.json` |
| Unit | `conduit/tests/test_pydantic_validator_smoke.py` (CliRunner) |
| CI | `.github/workflows/conduit-watch-demo.yml` jobs `pydantic-watch-dirty-fail` and `pydantic-watch-clean-pass` |

Packet rules: `DEPENDENCY_BUMP` to `2.0.0`, `AST_IMPORT_REWRITE` on the import-clause
fragment `BaseModel, validator` → `BaseModel, field_validator`, then
`AST_CALL_REWRITE` `validator` → `field_validator`. Import rewrite uses the clause
fragment on purpose. A bare `validator` string fallback would re-prefix
`field_validator` on a second apply.

`side_effects` name uncovered work: `.dict()`, inner `Config`, and
signature / `classmethod` reshape.

Catalog publish of this hop under `packets/` is deferred. The smoke loads the
committed sample-packet path. CLI lock is this slice.

## Inventory

| Bucket | Items |
|--------|-------|
| Watch-visible | Pin at `to_version` with leftover `old_callee` `validator` |
| Apply-only | Import-clause rewrite (`AST_IMPORT_REWRITE`); pin bump |
| Out-of-engine | `.dict()`, `class Config` / `orm_mode`, `@classmethod` / signature / `mode=` |

## Measured run (head of this PR)

Seed the fixture pin at `pydantic==2.0.0` so the first Watch is `bump_dirty`, not
`pre_bump`. Then apply and Watch again on the same tree.

| Step | Result |
|------|--------|
| CLI Watch at `to_version` with seeded leftover | exit 1 |
| CLI apply | exit 0; `@field_validator("name")`; pin `pydantic==2.0.0`; no `field_field_validator` |
| Second CLI apply | Model and requirements bytes unchanged |
| CLI Watch post | exit 0 |
| Residue `.dict(` count | 1 |
| Residue `class Config` count | 1 |

Commands used no LLM env vars. Pytest stubs `sync_bumped_packages` so the unit
path stays offline. CI runs real `conduit apply` including env sync.

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
