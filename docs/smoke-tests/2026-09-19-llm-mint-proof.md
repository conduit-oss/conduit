# Smoke: LLM-mint proof (2026-09-19)

**Status:** LIVE recorded (full-surface remint). Provider `openai`, model
`gpt-5.4-mini`. Frozen packet is rich: call-site rules include `validator` and
`dict` rewrites with binder-aligned `surface` floors. Config migration stays in
`side_effects` (prose), not mechanical rules.

Compares against P5's hand-authored `pydantic-validator-hop.json` on the same
fixture. P5 encodes the Watch-visible `validator` → `field_validator` hop. This
live remint encodes broader BaseModel + validator renames from the migration
guide so apply rewrites both `@validator` and `.dict()` on the fixture.

## LIVE section

### Inputs

| Item | Value |
|------|-------|
| Mint command | `conduit packet new --package pydantic --ecosystem pypi --version 2.0.0 --source-url https://docs.pydantic.dev/2.0/migration/ --out examples/sample-packet/pydantic-llm-mint-live.json` |
| Remint helper | `docs/smoke-tests/run-live-remint.py` → `p6-remint-full-surface.json` |
| Model / provider | `gpt-5.4-mini` / `openai` |
| Sources | `https://docs.pydantic.dev/2.0/migration/` (`kind: docs`) |
| Frozen packet | `examples/sample-packet/pydantic-llm-mint-live.json` |
| Packet SHA-256 | `a37c6dcff5fcd1621ce50bfeecbffda70ab64b78b6920cf768603126e70aca32` |
| Consumer | `examples/pydantic-validator-fixture/` (repo fixture; pin `pydantic==1.10.13`) |
| Consumer commit | head of this PR (`git rev-parse HEAD` at record time) |
| Replay | `docs/smoke-tests/replay-llm-mint-proof.py` |

Prior freezes (historical checksums only):

| Freeze | SHA-256 |
|--------|---------|
| Original live mint | `e5ab11b54ff4927ccf9d3e818a2bf1a3bd31c8ba736250f7bf0b493025e32a8d` |
| Surface-floor upgrade (same callees) | `fd2eba852dcbe2bc49384e9a41a3a7daabc1c162abb1ff1c91ec890e9fcf57df` |
| Full-surface remint (this freeze) | `a37c6dcff5fcd1621ce50bfeecbffda70ab64b78b6920cf768603126e70aca32` |

First mint with keys produced schema-invalid aliases (`old`/`new` instead of
`old_callee`/`new_callee`). That is IB-02 packet authoring. Mint normalize + prompt
field names were fixed, then mint was re-run. Surface floors were attached by mint
(`enrich_minted_rules`). Full remint regenerated call-site rules so `validator`
appears as an `AST_CALL_REWRITE`, not only as a P5 hand hop.

### Rule-family counts

| Family | Count |
|--------|-------|
| `DEPENDENCY_BUMP` | 1 |
| `AST_CALL_REWRITE` | 9 |
| `AST_ATTR_RENAME` | 2 |
| `AST_IMPORT_REWRITE` | 4 |
| Call-site (non-`DEPENDENCY_*`) | 15 |
| `AST_CALL_REWRITE` with `surface` | 9 |
| `side_effects` | 3 (Config / parse_raw / BaseSettings) |

Interesting `old_callee`s: `dict`, `json`, `copy`, `construct`, `parse_obj`,
`update_forward_refs`, `validator`, `root_validator`, `validate_arguments`.

### Measured run

Copy the fixture to a temp tree. Run Watch → apply → Watch. No enrich at apply
(published packet path). Keys were present for mint only.

| Step | Result |
|------|--------|
| Mint wall (informational) | ~224 s (`run-live-remint.py`) |
| `packet test` | exit 0 (16 rules) |
| Pre-apply Watch | exit 0; `status=pre_bump`; pin `1.10.13`; `leftover_count=1` (`validator`); `completeness=incomplete` (2 obs on `@validator`) |
| Apply | exit 0; pin → `pydantic==2.0.0`; `AST_CALL_REWRITE` `dict`→`model_dump`, `validator`→`field_validator`; no `packet enrichment`; no `SURFACE_DEFINITE_REWRITE` (AST call path fired) |
| Post-apply Watch | exit 0; `status=clean`; pin `2.0.0`; `leftover_count=0`; `completeness=complete` |
| Apply + Watch wall | 48.6 s (under 180 s rule; dominated by verify-venv install) |

### Leftover score

Packet-visible leftovers (Watch keys on packet `old_callee` / call-site rules):

| Moment | leftover_count | leftovers |
|--------|----------------|-----------|
| Pre-apply | 1 | `validator` |
| Post-apply | 0 | `[]` |

Manual residue on the fixture after apply:

| Token / pattern | Pre | Post |
|-----------------|-----|------|
| `@validator(` | 1 | 0 |
| `@field_validator(` | 0 | 1 |
| import name `validator` (combined `BaseModel, validator` line) | present | still present (exact `AST_IMPORT_REWRITE` miss) |
| `class Config` | 1 | 1 (side_effects / manual) |
| `.dict(` | 1 | 0 |
| `model_dump` | 0 | 1 |
| `orm_mode` | 1 | 1 |

### Dual verdict

| Gate | Verdict |
|------|---------|
| Gate validity (freeze → apply without re-enrich; pin hop; Watch leftovers) | PASS |
| Completeness vs *declared* surfaces on this fixture | PASS (`completeness: complete` post-apply; pre-apply `incomplete` on `@validator`) |
| Full fixture Config / combined-import polish | Outside mechanical remint (`side_effects` + known exact-import gap) |

### Thin-result follow-up

n/a (rich). Remint wrote schema-valid rules including `validator`. Config stays
prose. No apply vendor branch.

### Perf

| Metric | Value |
|--------|-------|
| Apply + Watch at head (this freeze) | 48.6 s (under 180 s rule) |
| Mint wall | ~224 s (informational) |

### Pytest

```text
python -m pytest -q conduit/tests/test_packet_author_cli.py conduit/tests/test_pydantic_validator_smoke.py conduit/tests/test_mint_surface_floor.py
```

## Surface upgrade proof (PR-A/B/C, historical)

Script: `docs/smoke-tests/run-prc-surface-upgrade.py` → `p6-prc-surface-upgrade.json`.
Re-run against the reminted freeze (same apply/Watch outcome; `surface_rewrite`
is false because remint AST call rules fire before definite surface rewrite).

| Step | Result |
|------|--------|
| Pre-apply completeness | `incomplete` (`@validator`) |
| Apply | exit 0; AST call rewrites for `dict` + `validator` |
| Post-apply Watch | `clean`; `completeness.status=complete` |

## SUPERSEDED: blocked stub (no keys)

**Status:** SUPERSEDED by the LIVE section above. Kept for Appendix C history when
keys were absent.

| Item | Value |
|------|-------|
| Frozen packet | `examples/sample-packet/pydantic-llm-mint-stub-blocked.json` |
| Packet SHA-256 | `c0c8c91f1139794137e55940bb2c26362b894d5adde5db6c4c43dac62d62c1d5` |
| Model / provider | none (LLM not configured) |
| Call-site rules | 0 (`DEPENDENCY_BUMP` only) |

Mint warning then: `LLM not configured; wrote dependency hop + sources only (no invented rules)`.
Do not treat the stub packet as rich.
