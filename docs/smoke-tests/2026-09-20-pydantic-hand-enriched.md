# Smoke: hand-enriched pydantic live packet (2026-09-20)

**Status:** HAND enrichment of missing rule families. Not an LLM remint.

The frozen mint at `examples/sample-packet/pydantic-llm-mint-live.json` stays a
recorded LLM artifact (this PR does not edit it; it still has no `validator`
hop and no `class Config` EXACT replace). This sibling
adds packet rules so the repo fixture's `@validator` and inner `class Config` /
`orm_mode` are in-scope. Amin remints with keys separately.

Apply / Watch stay generic. No pydantic-named branches in patcher or Watch.

## Inputs

| Item | Path |
|------|------|
| Fixture | `examples/pydantic-validator-fixture/` (`pydantic==1.10.13`, `@validator("name")`, `class Config` / `orm_mode`, `self.dict()`) |
| Live mint (unchanged) | `examples/sample-packet/pydantic-llm-mint-live.json` |
| Hand-enriched sibling | `examples/sample-packet/pydantic-llm-mint-live-hand-enriched.json` |
| Unit | `conduit/tests/test_pydantic_hand_enriched_smoke.py` |
| Replay | `docs/smoke-tests/replay-hand-enriched-pydantic.py` |
| CI | `.github/workflows/conduit-watch-demo.yml` job `pydantic-hand-enriched-apply-watch` |
| Hop copied from | `examples/sample-packet/pydantic-validator-hop.json` (P5) |

## Added rule families

Copied / expressed with existing schema types only:

| Family | What it covers on this fixture |
|--------|--------------------------------|
| `AST_IMPORT_REWRITE` | Clause fragment `BaseModel, validator` → `BaseModel, field_validator, ConfigDict` (P5 hop fragment so a second apply cannot emit `field_field_validator`) |
| `AST_CALL_REWRITE` + `surface` | `validator` → `field_validator` (`imported` + `decorator`); Watch leftovers key on `old_callee` |
| `EXACT_STRING_REPLACE` | Inner `class Config:` / `orm_mode = True` → `model_config = ConfigDict(from_attributes=True)` |

Live mint BaseModel call/attr/param rewrites and their `surface` floors are kept.

`side_effects` still name work this packet does not claim: other Config layouts,
`parse_raw` / `parse_file` / `schema_json`, `pydantic-settings`, and
`@classmethod` / signature / `mode=` reshape.

Live `BaseModel.dict` → `BaseModel.model_dump` is unchanged; definite surface
apply rewrites fixture `self.dict()` to `BaseModel.model_dump()` (receiver is
not preserved when `new_callee` is the dotted export). This sibling does not
add apply branches to change that.

## Measured run

Copy the fixture to a temp tree. Offline (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`
cleared). `conduit packet test` → `conduit apply` → `conduit watch --json`.

| Item | Value |
|------|-------|
| Hand-enriched SHA-256 | `081bfd68de58214e03d492ace56853e44cd4fa5215a6a1ca24aad33e4460046a` |
| `packet test` | exit 0; 21 rules |
| Apply | exit 0; pin `pydantic==2.0.0`; import clause + `@field_validator` + `model_config = ConfigDict(from_attributes=True)` + `model_dump`; no `field_field_validator`; no `packet enrichment` |
| Post-apply Watch | exit 0; `status=clean`; `leftover_count=0`; `completeness.status=complete` |
| Pytest | `7 passed` (`test_pydantic_hand_enriched_smoke.py` + hop smoke) |

## Dual verdict

| Gate | Verdict |
|------|---------|
| Gate validity (apply without re-enrich; Watch leftovers; completeness vs declared surfaces) | PASS |
| Full mergeable pydantic v2 / every Config shape | FAIL (see `side_effects`) |

## Pytest

```text
python -m pytest -q conduit/tests/test_pydantic_hand_enriched_smoke.py conduit/tests/test_pydantic_validator_smoke.py
```

## Replay

```text
python docs/smoke-tests/replay-hand-enriched-pydantic.py
```
