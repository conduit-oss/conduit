---
name: verify-conduit
description: >-
  Verify Conduit's CLI consumer-migration path toward a mergeable PR with
  minimal human review: watch → apply → watch on disposable trees, catalog
  publish→consume, and honest leftovers for permanent gaps. Use when proving
  consumer migration or checking what humans must still review — not for LLM
  mint authoring or UI review.
---

# Verify Conduit (consumer migration CLI)

Primary surface is the **`conduit` CLI** (`python -m conduit.main`). There is no web UI for apply/Watch. Producers author/publish packets; consumers run Watch and apply on their own trees. This skill proves the **consumer migration** path on isolated copies — never mutate `examples/` in place.

## Goal (operator)

Ship a **mergeable PR with minimal human changes**. Verification should:

1. Prove mechanical work is done (Watch clean on mapped shapes, or apply incomplete with explicit leftovers).
2. **Surface remaining human checks cleanly** (packet `side_effects`, Watch leftovers, apply exit 1 messages) — not bury them in logs.
3. Never claim unbounded full pydantic v2 or an arbitrary monorepo is “done” when permanent gaps remain.

## Launch

Conduit is a short-lived CLI (no long-running server).

1. Repo root must contain `conduit/src/conduit/`.
2. Put the package on `PYTHONPATH` (or install editable once):

```bash
# from repo root
export PYTHONPATH=conduit/src          # Windows PowerShell: $env:PYTHONPATH="conduit/src"
python -m conduit.main --help
```

Ready when `--help` lists `watch`, `apply`, `run`, and `packet`.

Optional editable install (same ready check):

```bash
python -m pip install -e "./conduit[dev]"
conduit --help
```

Teardown: nothing to stop. Only remove scratch trees via Cleanup.

## Doctor

Run before any drive when something looks off:

```bash
python .cursor/skills/verify-conduit/helpers/doctor.py
```

Require `doctor=PASS`, `conduit_import=ok`, and `packet_ok` / `tree_ok` lines for the fixtures you will drive. If import fails, set `PYTHONPATH=conduit/src` and retry.

## Drive

Harness = **shell** invoking `python -m conduit.main …` on an **isolated** consumer copy.

1. Isolate:

```bash
python .cursor/skills/verify-conduit/helpers/isolate_consumer.py \
  --source examples/pydantic-validator-fixture \
  --dest .cursor/skills/verify-conduit/scratch/my-run
```

2. Prefer the one-shot proof for the primary feature:

```bash
python .cursor/skills/verify-conduit/helpers/prove_watch_apply_watch.py
```

3. Or drive by hand (pydantic fixture kill bar):

```bash
# seed pin at to_version so first Watch is bump_dirty
python -m conduit.main watch --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json --json
# expect exit 1, status=bump_dirty, leftovers non-empty

python -m conduit.main apply --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json
# expect exit 0; tree rewritten

python -m conduit.main watch --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json --json
# expect exit 0, status=clean, leftover_count=0
```

Stable handles: CLI subcommands `watch` / `apply` / `run` / `packet publish`; packet paths under `examples/sample-packet/`; Watch `--json` fields `status`, `leftover_count`, `leftovers`, `exit_code` (also `pin`, `message`, optional `completeness`).

Read `features/README.md` and drive the feature file that matches the claim. Do not report a skipped entry point as verified through another path.

## Human checks (point these out)

When a migration is not fully mechanical, evidence must make the remaining review cheap:

| Signal | Where | Human action |
|--------|--------|--------------|
| Packet `side_effects` (`uncodable`, `multi_step`) | hop JSON | Read checklist; do not invent successors (`MAX_EMAIL_LENGTH`) |
| Watch `leftovers` at `to_version` | `watch --json` | Fix or accept redesign; PR not mergeable-clean while `bump_dirty` |
| Apply exit 1 “incomplete… leftovers remain” | apply stdout | Same as Watch leftovers — declaration refuse counts |
| `completeness.status: unverified` with `status: clean` | Watch JSON | Leftover-clean ≠ full surface proof; note if claiming completeness |

Paste leftover rows and `side_effects` into the PR body so review is a short list, not a log dig.

## Evidence

Proof root: `.cursor/skills/verify-conduit/evidence/<feature>-<UTC-stamp>/`.

Standards:

- Exercise the **real CLI** on a disposable consumer tree, not `rewrite_declarations` unit helpers alone.
- Capture **before Watch**, **apply**, **after Watch** (stdout/stderr/exitcode) plus `tree.before.txt` / `tree.after.txt`.
- Prove side effects in the tree alongside Watch verdict.
- For permanent-gap features, prove **not clean** / apply incomplete — that is success.
- `--dry-run` on apply must leave the tree unchanged — observe file bytes.
- No LLM keys required for these consumer proofs.

`prove_watch_apply_watch.py` writes a full evidence directory and `RESULT.txt`.

## Cleanup

```bash
python .cursor/skills/verify-conduit/helpers/cleanup_scratch.py --all
# or:
python .cursor/skills/verify-conduit/helpers/cleanup_scratch.py --path <absolute-scratch>
```

Removes only `verify-conduit/scratch/`. **Never** deletes `evidence/`. Never kill processes by name (CLI is short-lived).

## Helpers

| Script | Invocation |
|--------|------------|
| Doctor | `python .cursor/skills/verify-conduit/helpers/doctor.py` |
| Isolate tree | `python .cursor/skills/verify-conduit/helpers/isolate_consumer.py --source <rel> --dest <abs>` |
| Prove pydantic fixture migrate | `python .cursor/skills/verify-conduit/helpers/prove_watch_apply_watch.py` |
| Cleanup scratch | `python .cursor/skills/verify-conduit/helpers/cleanup_scratch.py --all` |

## Claims this skill will and will not make

| Claim | Verifiable here? |
|-------|------------------|
| Declared fixture migrates Watch-clean (mergeable mechanical bar) | Yes |
| Catalog publish hop+recipe → consume from catalog path | Yes |
| Permanent refuse stays visible (apply incomplete / Watch dirty) | Yes |
| Unbounded full pydantic v2 closed “done” | No — warn/refuse gaps remain |
| Arbitrary monorepo mergeable with zero human checks | Only if leftovers empty; else checks must be listed |

## Feature map

See [features/README.md](features/README.md).
