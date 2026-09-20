# Smoke: LLM-mint proof (2026-09-19)

**Status:** LIVE recorded. Provider `openai`, model `gpt-5.4-mini`. Frozen packet is
rich (call-site rules beyond `DEPENDENCY_*`). The earlier blocked stub remains only as
a superseded historical receipt for the no-keys Appendix C path.

Compares against P5's hand-authored `pydantic-validator-hop.json` on the same fixture.
P5 encodes the Watch-visible `validator` → `field_validator` hop. This live mint encodes
broader BaseModel rename rules from the migration guide. After the surface-floor upgrade,
typed `self.dict()` is rewritten via definite surface apply; `@validator` / `class Config`
remain outside this packet's declared surfaces.

**Hand enrichment (not a remint):** sibling
`examples/sample-packet/pydantic-llm-mint-live-hand-enriched.json` adds the P5
validator hop plus schema `EXACT_STRING_REPLACE` for the fixture `class Config` /
`orm_mode` block so those families are in-scope without mutating this LIVE
receipt. Amin remints with keys separately. See
[2026-09-20-pydantic-hand-enriched.md](./2026-09-20-pydantic-hand-enriched.md).

## LIVE section

### Inputs

| Item | Value |
|------|-------|
| Mint command | `conduit packet new --package pydantic --ecosystem pypi --version 2.0.0 --source-url https://docs.pydantic.dev/2.0/migration/ --out examples/sample-packet/pydantic-llm-mint-live.json` |
| Model / provider | `gpt-5.4-mini` / `openai` |
| Sources | `https://docs.pydantic.dev/2.0/migration/` (`kind: docs`) |
| Frozen packet | `examples/sample-packet/pydantic-llm-mint-live.json` |
| Packet SHA-256 | `fd2eba852dcbe2bc49384e9a41a3a7daabc1c162abb1ff1c91ec890e9fcf57df` |
| Consumer | `examples/pydantic-validator-fixture/` (repo fixture; pin `pydantic==1.10.13`) |
| Consumer commit | head of this PR (`git rev-parse HEAD` at record time) |
| Replay | `docs/smoke-tests/replay-llm-mint-proof.py` |

Surface floor upgrade (mint-surface PR): same call-site rules as the original live
mint; each `AST_CALL_REWRITE` now carries binder-aligned `surface` metadata so
completeness is proof-eligible. Prior checksum
`e5ab11b54ff4927ccf9d3e818a2bf1a3bd31c8ba736250f7bf0b493025e32a8d` is historical.

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
| `.dict(` | 1 | 0 |
| `model_dump` | 0 | 1 |
| `orm_mode` | 1 | 1 |

### Dual verdict

| Gate | Verdict |
|------|---------|
| Gate validity (freeze → apply without re-enrich; pin hop; Watch leftovers) | PASS |
| Completeness vs *declared* surfaces on this fixture | PASS (`completeness: complete` post-apply) |
| Full fixture v1 (`@validator`, `Config`) | Outside this packet (P5 covers validator hop) |

Pre-upgrade (lexical-only) completeness was `unverified`. After `surface` floors,
pre-apply bind is `incomplete` (`self.dict`); post-apply Watch is `complete`.

### Thin-result follow-up

n/a (rich). IB-02 normalize ran once before the original remint. Surface floors were
attached deterministically (no hand-authored fixture rules). No apply vendor branch.

### Perf

| Metric | Value |
|--------|-------|
| Apply + Watch at head (surface upgrade proof) | 54.1 s (under 180 s rule) |
| Trunk baseline | n/a (feature) |
| Mint wall | informational only (original ~300 s remint) |

### Pytest

```text
python -m pytest -q conduit/tests/test_packet_author_cli.py conduit/tests/test_pydantic_validator_smoke.py conduit/tests/test_mint_surface_floor.py
```

## Surface upgrade proof (PR-A/B/C)

Script: `docs/smoke-tests/run-prc-surface-upgrade.py` → `p6-prc-surface-upgrade.json`.

| Step | Result |
|------|--------|
| Upgrade | `enrich_minted_rules`; 10/10 AST_CALL_REWRITE gained `surface` |
| Pre-apply completeness | `incomplete` (1 obs: `self.dict`) |
| Apply | exit 0; `SURFACE_DEFINITE_REWRITE` `self.dict`→`self.model_dump` |
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
