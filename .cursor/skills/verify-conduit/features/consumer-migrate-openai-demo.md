# Migrate openai demo consumer

A tiny consumer still on `openai.ChatCompletion.create` is migrated offline with the sample packet. The documented kill bar is **`conduit run --demo --skip-tests --skip-pr`**, which applies packet rules plus OpenAI client-chain polish. Proves getting-started structural hop without LLM keys.

## Sub-features

- `demo-run` — `conduit run --demo --skip-tests --skip-pr` on an isolated demo-consumer copy (full kill bar).
- `apply-packet-only` — `conduit apply` rewrites packet surfaces but **does not** run `apply_openai_client_chain`.
- `watch-gate` — Watch against the sample packet after migration.
- `dry-run` — apply `--dry-run` leaves file bytes unchanged.

## How to get to it (user POV)

- Start from `examples/demo-consumer` (isolated copy).
- Use packet `examples/sample-packet/conduit-packet.json`.
- Prefer `conduit run … --demo --skip-tests --skip-pr` for the full demo kill bar.

## Driving it with conduit CLI

Preconditions:

- `doctor.py` PASS.
- Isolated copy of `examples/demo-consumer`.
- No need for `OPENAI_API_KEY` when `--demo` is set on `run`.

- **Isolate.** Run `python .cursor/skills/verify-conduit/helpers/isolate_consumer.py --source examples/demo-consumer --dest .cursor/skills/verify-conduit/scratch/openai-demo`.
- **Run demo pipeline (primary).** Run `python -m conduit.main run --path <scratch> --packet examples/sample-packet/conduit-packet.json --demo --skip-tests --skip-pr`. Exit code is `0`.
- **Confirm rewrite.** Read `<scratch>/src/ai_client.py`: no `ChatCompletion.create`; expect `OpenAI()` / `client.chat.completions.create` after **run**. Capture before/after under `evidence/consumer-migrate-openai-<UTC>/`.
- **Apply-only (narrower).** On a **fresh** isolate, run `python -m conduit.main apply --path <scratch> --packet examples/sample-packet/conduit-packet.json`. Exit `0`. Expect packet-level rewrite (`openai.chat.completions.create` possible) **without** requiring the full client-chain tree unless you also run polish via `run`.
- **Dry-run check.** On another fresh isolate, run apply with `--dry-run`. File bytes of `ai_client.py` must match pre-apply; exit `0`.
- **Proof.** Save command lines, exit codes, and `ai_client.py` before/after. Prefer `run` evidence when claiming the getting-started kill bar.

## Gotchas

- `--skip-pr` still **writes files**. It is not dry-run.
- Demo consumer tests may assert the legacy surface; `--skip-tests` is intentional for the structural kill bar.
- **`conduit apply` ≠ `conduit run` for openai:** only `run` applies `apply_openai_client_chain`. Do not claim identical trees from apply-only.
- Restore nothing under `examples/demo-consumer`; only delete scratch.
