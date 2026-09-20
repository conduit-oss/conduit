# Smoke: LLM-mint proof (2026-09-19)

**Status:** LIVE recorded. Provider `openai`, model `gpt-5.4-mini`. Frozen packet is
rich (call-site rules beyond `DEPENDENCY_*`). The earlier blocked stub remains only as
a superseded historical receipt for the no-keys Appendix C path.

Compares against P5's hand-authored `pydantic-validator-hop.json` on the same fixture.
P5 encodes the Watch-visible `validator` → `field_validator` hop. This live mint encodes
broader BaseModel rename rules from the migration guide and does not rewrite the
fixture's `@validator` / `class Config` / short-name `.dict()` surface.

## LIVE section

### Inputs

| Item | Value |
|------|-------|
| Mint command | `conduit packet new --package pydantic --ecosystem pypi --version 2.0.0 --source-url https://docs.pydantic.dev/2.0/migration/ --out examples/sample-packet/pydantic-llm-mint-live.json` |
| Model / provider | `gpt-5.4-mini` / `openai` |
| Sources | `https://docs.pydantic.dev/2.0/migration/` (`kind: docs`) |
| Frozen packet | `examples/sample-packet/pydantic-llm-mint-live.json` |
| Packet SHA-256 | `e5ab11b54ff4927ccf9d3e818a2bf1a3bd31c8ba736250f7bf0b493025e32a8d` |
| Consumer | `examples/pydantic-validator-fixture/` (repo fixture; pin `pydantic==1.10.13`) |
| Consumer commit | head of this PR (`git rev-parse HEAD` at record time) |
| Replay | `docs/smoke-tests/replay-llm-mint-proof.py` |

First mint with keys produced schema-invalid aliases (`old`/`new` instead of
`old_callee`/`new_callee`). That is IB-02 packet authoring. Mint normalize + prompt
field names were fixed, then mint was re-run once. Remint wrote a schema-valid rich
packet. No apply vendor branch was added.

### Rule-family counts

| Family | Count |
|--------|-------|
| `DEPENDENCY_BUMP` | 1 |
| `AST_CALL_REWRITE` | 10 |
| `AST_ATTR_RENAME` | 3 |
| `AST_PARAM_RENAME` | 3 |
| `AST_IMPORT_REWRITE` | 1 |
| Call-site (non-`DEPENDENCY_*`) | 17 |
| `side_effects` | 3 |

### Measured run

Copy the fixture to a temp tree. Run Watch → apply → Watch. No enrich at apply
(published packet path). Keys were present for mint only.

| Step | Result |
|------|--------|
| Mint wall (informational) | ~300 s remint after IB-02 normalize fix (first attempt ~192 s, schema-invalid) |
| `packet test` | exit 0 |
| Pre-apply Watch | exit 0; `status=pre_bump`; pin `1.10.13`; `leftover_count=0` |
| Apply | exit 0; pin → `pydantic==2.0.0`; no `packet enrichment` log; AST rules did not rewrite fixture body (`BaseModel.dict` vs short `.dict()`) |
| Post-apply Watch | exit 0; `status=clean`; pin `2.0.0`; `leftover_count=0` |
| Apply + Watch wall | 44.1 s (under 180 s rule; dominated by verify-venv install) |

### Leftover score

Packet-visible leftovers (Watch keys on packet `old_callee` / call-site rules):

| Moment | leftover_count | leftovers |
|--------|----------------|-----------|
| Pre-apply | 0 | `[]` |
| Post-apply | 0 | `[]` |

Manual residue on the fixture after apply (completeness, not Watch-visible for this packet's callees):

| Token / pattern | Pre | Post |
|-----------------|-----|------|
| `@validator(` / import `validator` | 1 / present | 1 / present |
| `class Config` | 1 | 1 |
| `.dict(` | 1 | 1 |
| `model_dump` | 0 | 0 |
| `orm_mode` | 1 | 1 |

### Dual verdict

| Gate | Verdict |
|------|---------|
| Gate validity (freeze → apply without re-enrich; pin hop lands; Watch matches packet rules) | PASS |
| Migration completeness (rich LLM mint covers fixture v1 surface / mergeable pydantic v2) | FAIL |

Completeness fails because the live mint's call-site rules do not hit this fixture's
`validator`, inner `Config`, or short-name `.dict()` usages. Gate validity still passes
because the pin hop lands and Watch finds no leftovers for the packet's declared
`old_callee` values. P5's frozen hop remains the fixture-visible comparison shape.

### Thin-result follow-up

n/a (rich). IB-02 normalize ran once before the remint because the first live packet
failed `packet test`. No inventing of fixture-specific AST rules by hand. No apply
vendor branch.

### Perf

| Metric | Value |
|--------|-------|
| Apply + Watch at head | 44.1 s (under 180 s rule) |
| Trunk baseline | n/a (feature) |
| Mint wall | informational only (~300 s remint) |

### Pytest

Related mint / pydantic smoke green on this branch tip:

```text
python -m pytest -q conduit/tests/test_packet_author_cli.py conduit/tests/test_pydantic_validator_smoke.py
```

## Retest on main after surface binder (#53)

**Recorded:** 2026-09-19 (local). Head `69e5b6f` (merge of #53). Same frozen live
packet (checksum unchanged). No remint. Keys loaded from `.env` for process env only
(mint not re-run).

Script: `docs/smoke-tests/retest-llm-mint-surface53.py` →
`docs/smoke-tests/p6-retest-surface-53.json`.

| Step | Result |
|------|--------|
| `packet test` | exit 0 |
| Apply | exit 0; `[SURFACE_DEFINITE_REWRITE] self.dict -> self.model_dump (1x)` |
| Post-apply Watch | exit 0; `status=clean`; `leftover_count=0` |
| `completeness` JSON | `unverified` (lexical-only contracts); `observation_count=0` after rewrite |
| Apply + Watch wall | 64.8 s (under 180 s) |

### Residue delta (fixture `src/model.py`)

| Token / pattern | Pre (#52 receipt) | Post (#53 retest) |
|-----------------|-------------------|-------------------|
| `@validator(` | 1 | 1 |
| `class Config` | 1 | 1 |
| `.dict(` | 1 | **0** |
| `model_dump` | 0 | **1** |
| `orm_mode` | 1 | 1 |

### Dual verdict (updated)

| Gate | Verdict |
|------|---------|
| Gate validity (freeze → apply without re-enrich; pin hop; Watch leftovers) | PASS |
| Migration completeness vs full fixture v1 surface | FAIL (still: `@validator`, `Config` / `orm_mode`) |
| `BaseModel.dict` / typed `self.dict` hop | PASS on this fixture after #53 definite rewrite |

Completeness JSON stays `unverified` because the frozen LLM packet is still
lexical-only (`old_callee` strings), not proof-eligible `SurfaceContract`s. The
rewrite path still clears the definite `self.dict` observation. P5 remains the
comparison shape for the Watch-visible validator hop.

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
