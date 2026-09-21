# Full migration stage-2 plan

Goal for the consumer and the next maintainer. Remint authors apply-ready packets from evidence until a real package upgrade is mechanical. Apply runs rules. Side_effects hold only gaps current ops cannot express. No vendor invent in cook.

This plan follows stage 1 (strip invent, doc chunks, prompt contract, coverage pass) on `stack/import-member-cook`.

## How to read this

One box is one unit of work with named evidence. Body is how-to. Appendices hold explanation.

Execution playbook when armed. `pstack/skills/poteto-mode/playbooks/autopilot-stack.md` (stack, operator lands).

Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

## Program checklist

### Arm the program

- [x] State this plan to the operator, then stop until explicit go.
- [x] On go, arm a `/goal` with path, PR order, verification rule, who merges, done condition.
- [x] Re-read execution playbook, swarm, control-cli, opening-a-pr from trunk each tick.

### Spawn owners

- [x] PR-A structured side_effects (schema) after stage 1 merges.
- [x] PR-B reshape declaration kinds after PR-A (or parallel if files disjoint).
- [x] PR-C consumer-rooted remint after PR-B or independent if CLI-only.
- [x] PR-D live remint prove + fixture score after A–C land as needed.

### Dependency graph

- [ ] PR-A from `main`. Schema + normalize for structured gaps.
- [ ] PR-B from PR-A head (or main if A merged). New declaration ops + apply/Watch residuals.
- [ ] PR-C from main (CLI path). Optional stack on B if it consumes structured gaps.
- [ ] PR-D remint prove on tip.

## PR-A Structured side_effects

**Depends on.** Stage 1 merged.

**Files.**

- [ ] Edit `schema/conduit-packet.schema.json` and `conduit/schema/conduit-packet.schema.json`.
- [ ] Edit `conduit/src/conduit/packet/synthesize.py` normalize for structured rows.
- [ ] Edit validate + tests.

**Build.**

- [ ] Extend `side_effects[]` with optional structured fields (keep `kind` + `detail` required for back-compat). Suggested shape. `gap_kind` (enum), `old_shape`, `new_shape`, `blocker` (why not a rule), `evidence_url`. Free prose `detail` remains the human summary.

**You see.**

- [ ] Remint can emit structured gaps. Old packets with only `{kind, detail}` still validate.

**Verify, unit.**

- [ ] Schema + normalize tests for structured and legacy rows. Run `pytest -q conduit/tests/test_packet_author_cli.py`.

**Verify, live.**

- [ ] Lane 1. Regression. `packet test` on hop + live packets at trunk and head. Pass when both exit 0.
- [ ] Lane 2. Remint dry sample emits at least one structured side_effect when a multi-step gap remains. Pass when schema validates.
- [ ] Lanes 3–10. CLI `packet new --scaffold-only` and `packet test` on sample packets. Pass when exit 0.

**Verify, perf.**

- [ ] Metric. Remint wall-clock for pydantic migration URL (informational budget 600s).
- [ ] Probe. `docs/smoke-tests/run-live-remint.py` once at head (keys required).
- [ ] Baseline. Record prior stage-1 remint wall if available.
- [ ] Rule. Head finishes under 600s or document key/network failure.

**Review gate.** None. Schema-only.

**Merge.** Squash after clean swarm.

## PR-B Reshape declaration kinds

**Depends on.** PR-A.

**Files.**

- [ ] Edit declaration schema + `packet/declaration_rules.py`.
- [ ] Edit `patcher/declarations/*` apply + residual scan.
- [ ] Edit Watch leftovers wire.
- [ ] Tests + hop/fixture updates if a reshape covers validator classmethod.

**Build.**

- [ ] Add one closed operation kind for multi-edit hops (name TBD in architect arena). First target. decorator rename + ensure classmethod (or mode=) when evidence states it. Packet carries the op. Apply stays deterministic.
- [ ] Remint-time preferred. LLM emits the new op when evidence supports it. Otherwise structured side_effect (PR-A).

**You see.**

- [ ] Fixture or micro-fixture with `@validator` that needs classmethod becomes mechanically clean after apply when the packet declares the reshape op.

**Verify, unit.**

- [ ] Declaration apply + residual tests. Run `pytest -q conduit/tests/test_declaration_rewrite.py`.

**Verify, live.**

- [ ] Lane 1. Regression against trunk hop smoke.
- [ ] Lane 2. New reshape fixture watch→apply→watch. Pass when leftovers empty and classmethod present if required.
- [ ] Lanes 3–10. control-cli on `conduit apply` / `conduit watch` for hop + reshape fixture.

**Verify, perf.**

- [ ] Metric. Apply wall on reshape fixture.
- [ ] Probe. CliRunner apply twice (idempotent).
- [ ] Baseline. Hop apply wall from existing smoke.
- [ ] Rule. Head within 2x hop apply or under 30s absolute.

**Review gate.** Operator reviews apply diff screenshots or printed file before/after in chat.

**Merge.** After clean swarm.

## PR-C Consumer-rooted agent remint

**Depends on.** None hard. Prefer after PR-A so gaps can be structured.

**Files.**

- [ ] Edit CLI `packet new` to accept optional `--path` consumer root.
- [ ] Edit `synthesize_from_evidence` call sites to pass `root` when set (enables `run_agent`).
- [ ] Docs + tests.

**Build.**

- [ ] When `--path` is set, remint uses multi-turn agent tools against that tree. Link-only remint unchanged when omitted.

**You see.**

- [ ] Remint with `--path examples/pydantic-validator-fixture` cites real usages in reasons and covers observed callees.

**Verify, unit.**

- [ ] Author tests with fake root + stubbed client.

**Verify, live.**

- [ ] Lane 1. Link-only remint still works (regression).
- [ ] Lane 2. Path remint on fixture produces surface rules. Pass when receipt ok.
- [ ] Lanes 3–10. CLI help and error paths for bad `--path`.

**Verify, perf.**

- [ ] Metric. Path remint wall under 900s.
- [ ] Probe. `conduit packet new ... --path <fixture>`.
- [ ] Baseline. Link-only remint wall.
- [ ] Rule. Document ratio. Fail only on hang over 900s.

**Review gate.** None.

**Merge.** After clean swarm.

## PR-D Full-migration prove

**Depends on.** PR-B (reshape) and preferably PR-C.

**Files.**

- [ ] Remint live packet.
- [ ] Smoke docs + checksum.
- [ ] Scorecard. rule families vs side_effects count vs fixture apply/Watch.

**Build.**

- [ ] Remint from migration guide. Prefer path remint on a larger consumer when available.
- [ ] Record what remains in structured side_effects (true uncodable).
- [ ] Dual verdict. Gate (packet leftovers clean) vs completeness (full guide coverage).

**You see.**

- [ ] Fixture green for all hops expressible by current + reshape ops. Remaining side_effects are explicitly multi-step.

**Verify, unit / live / perf.** As remint smoke + apply/Watch. Ten CLI lanes on watch/apply/packet test.

**Review gate.** Operator reviews scorecard in chat.

**Merge.** Docs + freeze only after operator accept.

## Close the program

- [ ] Every box checked with evidence.
- [ ] Reply with stack URLs and scorecard.

## Appendix A. Prototype evidence

Stage 1 showed match/replace normalize unblocks remint. Config invent was wrong. Doc chunking and coverage pass are the next levers (implemented in stage 1 code).

## Appendix B. Alternatives rejected

- Cook invent of ConfigDict from prose. Violates packets-carry-rules.
- Apply-LLM as the only path for reshape. Prefer remint-time rules first. Structured side_effects feed apply-LLM only for leftovers.
- Named dict/validator/Config as universal remint gate. Hop-proof only.

## Appendix C. Risks

- Reshape op design sprawl. Mitigate with arena for one op first.
- Path remint cost and tool loops. Cap turns. Keep link-only default.
- LLM still parks mechanical hops in side_effects. Coverage pass + structured gap_kind make that visible.

## Appendix D. Links

- Stage 1 plan. `.cursor/plans/strip_vendor_invent_82d7f3bc.plan.md`
- Hop packet. `examples/sample-packet/pydantic-validator-hop.json`
- Live packet. `examples/sample-packet/pydantic-llm-mint-live.json`
- Declaration engine. `conduit/src/conduit/patcher/declarations/`
