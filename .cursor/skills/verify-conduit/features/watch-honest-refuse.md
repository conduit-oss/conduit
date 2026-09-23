# Watch refuses unmapped shapes honestly

A consumer using `@field_validator(..., each_item=True)` hits a permanent `no_successor` gap. Apply does not rewrite it; leftovers include declaration residuals; at `to_version` Watch is `bump_dirty`. Success for this feature is a **clear human check list**, not a clean merge.

## Sub-features

- `each-item-present` — isolated `each_item.py` (or whole convention fixture) with `each_item=True`.
- `apply-incomplete` — normal apply exits **1** when declaration residuals remain (same leftover scanner as Watch).
- `watch-bump-dirty` — Watch at `to_version` reports leftovers; not `status=clean`.
- `human-checklist` — leftover rows + packet `side_effects` are what a reviewer must see.

## How to get to it (user POV)

- Use `examples/pydantic-convention-fixture/` (prefer isolating `each_item` alone to avoid sibling rewrites).
- Apply `pydantic-surface-hop.json`.
- Read apply failure / Watch `--json` leftovers.

## Driving it with conduit CLI

Preconditions:

- `doctor.py` PASS.
- Isolated one-file tree: copy `each_item.py` plus `requirements.txt` with `pydantic==2.0.0` (avoids clean siblings masking the gate).

- **Isolate.** Create scratch with `src/each_item.py` from the fixture and `requirements.txt` = `pydantic==2.0.0`.
- **Apply.** Run `python -m conduit.main apply --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json`. Expect exit **1** (“apply incomplete… leftovers remain”) with `each_item=True` still in source — not a silent clean apply.
- **Watch.** Run `python -m conduit.main watch --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json --json`. Expect `status=bump_dirty`, `exit_code=1`, `leftover_count ≥ 1`. Leftover rows may use declaration shapes (message text may still say “call(s)”).
- **Human checks.** Copy `leftovers` JSON and the hop’s `side_effects` (`always`/`each_item` multi_step, `MAX_EMAIL_LENGTH` uncodable) into evidence as the PR checklist.
- **Proof.** Evidence must include post-apply file still showing `each_item=True`, apply exit 1 (or Watch bump_dirty), and the leftover list. Do **not** assert `status=clean`.

## Gotchas

- Sibling files (`pre_true.py`, `trailing_values.py`) may rewrite; isolate `each_item` alone for a single-file dirty gate.
- Permanent refuse is success for this feature; do not “fix” it by deleting the kwarg by hand.
- Pre-bump pin (`1.10.13`) can yield Watch exit 0 with leftovers still listed — honesty for merge is at `to_version`.
- Declaration residuals **are** included in Watch leftovers (`scan_packet_leftovers`); do not claim Watch only sees `old_callee`.
