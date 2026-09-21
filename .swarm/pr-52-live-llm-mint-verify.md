# Root swarm verify: PR #52

**Verdict: CLEAN** at `311f55f4170dfc71a54f2cd6f1f1c04fdd873e3a`

- Repo: `conduit-oss/conduit`
- PR: https://github.com/conduit-oss/conduit/pull/52
- Branch: `stack/live-llm-mint-proof`
- Base: `main` @ `d91199f934089f35b7453b5a779bfc53278c2d81`
- Worktree: `D:\bye\conduit_repo\conduit-live-llm-mint` (hard-reset, HEAD match)
- Owners must not merge. PR body distrusted. No merge/push.

## Must-pass

| # | Check | Result | Evidence |
|---|--------|--------|----------|
| 1 | Diff scoped (mint synthesize/author + tests + smoke docs + sample packets; no apply/Watch vendor) | PASS | 7 files vs base. Paths: `author.py`, `synthesize.py`, `test_packet_author_cli.py`, smoke md + replay, live + stub sample packets. |
| 2 | Frozen live packet has call-site rules beyond `DEPENDENCY_*` (AST_* ≥ 1) | PASS | `pydantic-llm-mint-live.json`: AST_* = 17 |
| 3 | Receipt LIVE: provider/model named (no secrets); dual verdict gate PASS / completeness FAIL; checksum matches bytes | PASS | `openai` / `gpt-5.4-mini`; PASS/FAIL; SHA-256 `e5ab11b5…e32a8d` matches committed file; no secret patterns |
| 4 | Replay `python docs/smoke-tests/replay-llm-mint-proof.py` exit 0 | PASS | Worktree `.venv` (no `conduit/` Scripts path). Exit 0. Checksum ok. apply+watch ~31.7s clean. |
| 5 | Targeted pytest | PASS | 23 passed (`test_packet_author_cli.py` + `test_pydantic_validator_smoke.py`) |
| 6 | `normalize_llm_rules` aliases → schema fields | PASS | Pytest `test_normalize_llm_rule_aliases_match_schema` + direct import check |
| 7 | CI on head green | PASS | All head check_runs completed success, including `test` |

## Patch id

`671f38875b4676e7d846d3aa53d27883d843a1b7` (stable, base…HEAD)

## CI detail

Check runs on head (measured): `test`, `watch-clean-pass`, `watch-dirty-fail`, `watch-pin-past-dirty-fail`, `pydantic-watch-clean-pass`, `pydantic-watch-dirty-fail` all `success`.
