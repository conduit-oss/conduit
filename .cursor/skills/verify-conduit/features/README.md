# Conduit consumer-migration verification map

Maintained source for verifying user-facing consumer migration via the Conduit CLI toward a **mergeable PR with minimal human review**. Read this index, then the matching feature file.

## Baseline preconditions

- Repo root has `conduit/src/conduit/` and `PYTHONPATH=conduit/src` (or an editable install).
- `python .cursor/skills/verify-conduit/helpers/doctor.py` prints `doctor=PASS`.
- Every consumer tree is an **isolated copy** under `.cursor/skills/verify-conduit/scratch/` (or another disposable path). Never rewrite `examples/` in place.
- Frozen packets come from `examples/sample-packet/` (or a local catalog path produced by `packet publish`).
- No LLM keys required for these features.
- Never drive a shared checkout that another agent or human is editing.

## Driving conventions

- Start from an isolated copy of the named fixture unless the feature says otherwise.
- Prefer `python -m conduit.main` so the drive matches an uninstalled PATH layout.
- Treat every command as literal; keep packet paths and `--json` unchanged.
- Capture exit codes and Watch JSON (`status`, `leftover_count`, `leftovers`, `exit_code`) plus a second look at rewritten source files.
- When leftovers or `side_effects` remain, copy them into evidence as the **human check list** for a PR.
- Restore nothing under `examples/`; only delete scratch via `cleanup_scratch.py`. Keep `evidence/`.

## Proof and skip reporting

- Proof = user CLI path + resulting Watch/apply verdict + tree side effects.
- Mutation proof includes a second Watch (or apply-incomplete transcript) and a read of the source file.
- Record the feature ID in the evidence directory name.
- If a path is unreachable (missing packet, doctor FAIL), report the unmet precondition — do not substitute pytest unit helpers and call it the same feature.

## Feature entry contract

Each feature file: H1 + one paragraph, then exactly four H2s — `Sub-features`, `How to get to it (user POV)`, `Driving it with conduit CLI`, `Gotchas`.

## Features

- [Migrate pydantic validator fixture](./consumer-migrate-pydantic.md) — watch → apply → watch clean on the surface hop (mechanical mergeable bar).
- [Migrate openai demo consumer](./consumer-migrate-openai-demo.md) — offline demo `run` kill bar (`apply` alone is narrower).
- [Publish hop then consume from catalog](./catalog-publish-then-consume.md) — local catalog write + apply from catalog hop path.
- [Watch refuses unmapped shapes honestly](./watch-honest-refuse.md) — `each_item` → apply incomplete / Watch `bump_dirty` (human check list).
