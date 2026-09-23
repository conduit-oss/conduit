# Migrate pydantic validator fixture

A consumer stuck on pydantic v1 validator/`dict`/Config shapes loads a frozen migration hop (surface-authored), runs Watch (dirty at `to_version`), applies the hop, then Watch is clean with rewritten sources — the mechanical bar for a low-review mergeable PR on this fixture.

## Sub-features

- `seed-bump-dirty` — pin at `to_version` with leftover sites → Watch exit 1, `status=bump_dirty`.
- `apply-hop` — `conduit apply` rewrites validator, classmethod, Config, dict.
- `watch-clean` — second Watch exit 0, `status=clean`, `leftover_count=0`.
- `tree-proof` — source shows `@field_validator`, `@classmethod`, `model_dump`, `model_config`/`ConfigDict`, no `class Config`.

## How to get to it (user POV)

- Copy `examples/pydantic-validator-fixture` aside (or use the prove helper).
- Point `--packet` at `examples/sample-packet/pydantic-surface-hop.json`.
- Run `conduit watch`, then `conduit apply`, then `conduit watch` again.

## Driving it with conduit CLI

Preconditions:

- `doctor.py` is PASS.
- Packet `examples/sample-packet/pydantic-surface-hop.json` exists.
- Working tree is an isolated copy (not `examples/` itself).

- **One-shot proof.** Run `python .cursor/skills/verify-conduit/helpers/prove_watch_apply_watch.py`. Evidence lands under `evidence/consumer-migrate-pydantic-<UTC>/` with `RESULT.txt` = `PASS`.
- **Isolate manually.** Run `python .cursor/skills/verify-conduit/helpers/isolate_consumer.py --source examples/pydantic-validator-fixture --dest .cursor/skills/verify-conduit/scratch/pydantic-manual`. Write `pydantic==2.0.0` into that copy’s `requirements.txt`.
- **Watch dirty.** Run `python -m conduit.main watch --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json --json`. Exit code is `1`; JSON `status` is `bump_dirty`; leftovers may include call, import_member, and Config declaration rows (not only `validator`).
- **Apply.** Run `python -m conduit.main apply --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json`. Exit code is `0`.
- **Watch clean.** Run the same Watch command again. Exit code is `0`; JSON `status` is `clean` and `leftover_count` is `0`. Optional `completeness.status: unverified` may still appear — leftover-clean is the merge bar here, not full surface completeness.
- **Proof.** Read `<scratch>/src/model.py`: `@field_validator`, `@classmethod`, `model_dump`, `model_config` or `ConfigDict`, and no `class Config` / bare `@validator("name")`. Save transcripts under `evidence/`.

## Gotchas

- Leaving the pin at `1.10.13` makes first Watch `pre_bump` (exit 0) — that is not the bump_dirty kill bar. Seed `pydantic==2.0.0` first (the prove helper does this).
- Prefer `pydantic-surface-hop.json` over the narrower `pydantic-validator-hop.json` when claiming the producer-surface path (pytest smoke may still use the narrower hop).
- Apply may create `<scratch>/.conduit/` (venv, baseline). That is expected; cleanup removes the whole scratch tree.
- Packet `side_effects` still list permanent gaps (`MAX_EMAIL_LENGTH`, `always`/`each_item`). This feature does not clear them — list them as human checks if the PR claims beyond this fixture.
- This feature does **not** prove unbounded pydantic v2 or a third-party monorepo.
