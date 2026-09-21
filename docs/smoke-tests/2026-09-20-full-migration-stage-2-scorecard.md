# Full-migration stage-2 scorecard

Program: structured side_effects → ensure_classmethod reshape → consumer `--path`
remint → prove. Plan: `conduit-docs/full-migration-stage-2-plan.md`.

## Stack

| PR | Branch | URL |
|----|--------|-----|
| A structured side_effects | `stack/structured-side-effects` | https://github.com/conduit-oss/conduit/pull/64 |
| B ensure_classmethod | `stack/ensure-classmethod-reshape` | https://github.com/conduit-oss/conduit/pull/65 |
| C packet new --path | `stack/consumer-path-remint` | https://github.com/conduit-oss/conduit/pull/66 |
| D prove + scorecard | `stack/full-migration-prove` | (this branch) |

## Gate vs completeness

| Gate | Result | Evidence |
|------|--------|----------|
| Hop packet receipt surface ok | PASS | `check-remint-receipt.py --packet examples/sample-packet/pydantic-validator-hop.json` |
| Fixture watch→apply→watch clean | PASS | `test_pydantic_validator_smoke.py` |
| Expressible hops mechanical (dict, validator+classmethod, Config) | PASS | hop packet + ensure_classmethod after CALL |
| Remaining gaps structured | PASS | hop `side_effects[0].gap_kind == signature` |
| Live remint freeze refreshed | FAIL (thin) | wall 806.7s exit 1; prior freeze SHA `0e12cabe…` restored |
| Full guide coverage / mergeable consumer | FAIL | expected until more reshape ops + larger consumer |

## Rule family counts (hop freeze)

| Family | Count |
|--------|------:|
| DEPENDENCY_BUMP | 1 |
| AST_DECLARATION_REWRITE | 3 |
| AST_CALL_REWRITE | 2 |
| side_effects (structured) | 1 |

## Live remint

Path remint on `examples/pydantic-validator-fixture` via `run-live-remint.py`
(`--path` when fixture exists). **2026-09-20 run:** wall 806.7s, exit 1, thin
refuse after enrich+retry (agent tools: grep only). Prior
`pydantic-llm-mint-live.json` freeze kept. Record in `p6-remint-full-surface.json`.

## Dual verdict (operator)

| Gate | Verdict |
|------|---------|
| Packet leftovers clean for expressible hops | PASS (hop) |
| Full migration completeness | FAIL (live remint thin; signature/mode and guide breadth remain) |
