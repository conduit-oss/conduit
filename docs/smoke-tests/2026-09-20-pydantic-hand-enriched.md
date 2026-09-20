# Smoke: hand-enriched pydantic live packet (2026-09-20)

**Status:** HAND enrichment of missing rule families. Not an LLM remint.

The frozen mint at `examples/sample-packet/pydantic-llm-mint-live.json` stays a
recorded LLM artifact (SHA-256
`fd2eba852dcbe2bc49384e9a41a3a7daabc1c162abb1ff1c91ec890e9fcf57df`). This sibling
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
