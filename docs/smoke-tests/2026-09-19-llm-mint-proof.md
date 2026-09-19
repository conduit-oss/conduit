# Smoke: LLM-mint proof (2026-09-19)

**Status:** BLOCKED for live-model mint. No `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` /
provider keys in the owner environment. Appendix C path: stubbed `packet new`
(no LLM) still freezes, applies, and Watches. Live-model receipt parked until keys
exist. Do not treat this packet as rich.

Compares against P5's hand-authored `pydantic-validator-hop.json` shape on the
same fixture. This stub is pin-only. P5's hop carries call-site rules.

## Inputs

| Item | Value |
|------|-------|
| Mint command | `conduit packet new --package pydantic --ecosystem pypi --version 2.0.0 --source-url https://docs.pydantic.dev/2.0/migration/ --out examples/sample-packet/pydantic-llm-mint-stub-blocked.json` |
| Model / provider | none (LLM not configured) |
| Sources | `https://docs.pydantic.dev/2.0/migration/` (`kind: docs`) |
| Frozen packet | `examples/sample-packet/pydantic-llm-mint-stub-blocked.json` |
| Packet SHA-256 | `097b9e92cabe1c3dc20b4aa0b62e8d4ec926a3d38d694621e8b6a58077cea002` |
| Consumer | `examples/pydantic-validator-fixture/` (repo fixture; pin `pydantic==1.10.13`) |
| Consumer commit | head of this PR (`git rev-parse HEAD` at record time) |
| Replay | `docs/smoke-tests/replay-llm-mint-proof.py` |

Mint warning captured: `LLM not configured; wrote dependency hop + sources only (no invented rules)`.

## Rule-family counts

| Family | Count |
|--------|-------|
| `DEPENDENCY_BUMP` | 1 |
| `AST_*` / call-site | 0 |
| `side_effects` | 0 |

## Measured run

Copy the fixture to a temp tree. Run Watch → apply → Watch. No enrich at apply
(published packet path). Env had no LLM keys.

| Step | Result |
|------|--------|
| Mint wall (informational) | ~9 s including URL fetch; remint without keys stays pin-only |
| `packet test` | exit 0 |
| Pre-apply Watch | exit 0; `status=pre_bump`; pin `1.10.13`; `leftover_count=0` (packet has no call-site rules) |
| Apply | exit 0; pin → `pydantic==2.0.0`; log includes `pin-only: no call-site rules`; no enrich |
| Post-apply Watch | exit 0; `status=no_rules`; pin `2.0.0`; `leftover_count=0` |
| Apply + Watch wall | 30.1 s on rebased P3+P5 tip (dominated by verify-venv install) |

### Leftover score

Packet-visible leftovers (Watch keys on packet `old_callee` / call-site rules):

| Moment | leftover_count | leftovers |
|--------|----------------|-----------|
| Pre-apply | 0 | `[]` |
| Post-apply | 0 | `[]` |

Manual residue on the fixture after apply (completeness, not Watch-visible for this thin packet):

| Token / pattern | Pre | Post |
|-----------------|-----|------|
| `@validator(` / import `validator` | 1 | 1 |
| `class Config` | 1 | 1 |
| `.dict(` | 1 | 1 |

## Dual verdict

| Gate | Verdict |
|------|---------|
| Gate validity (freeze → apply without re-enrich; pin hop lands; Watch matches packet rules) | PASS |
| Migration completeness (rich LLM mint / mergeable pydantic v2) | FAIL |

Completeness fails because enrich never ran. The stub is honest pin-only. P5's
frozen hop is the call-site comparison shape, not this file.

## Thin-result follow-up

Not IB-01 / IB-02. Enrich was skipped for missing keys, not a thin LLM result.
Parked: re-run the same mint with keys, freeze a new packet if rich, replace this
receipt's live-model section. No apply vendor branch was added.

## Perf

| Metric | Value |
|--------|-------|
| Apply + Watch at head | 30.1 s (under 180 s rule) |
| Trunk baseline | n/a (feature) |
| Mint wall | informational only |

## Pytest

Related mint / pydantic smoke still green on this branch tip:

```text
python -m pytest -q conduit/tests/test_pydantic_validator_smoke.py
```
