# Assist redesign on refuse leftovers

Opt-in `--assist-redesign` proposes multi_step redesigns (`each_item` / `always`) after mechanical apply. Packets still declare `no_successor`. Good-to-go is leftover rescan + human checklist — not “assist ran.”

## Sub-features

- `no-flag-honesty` — without the flag, `each_item` stays apply-incomplete / Watch dirty with Human checks.
- `stub-assist-clears` — deterministic proposer drops `each_item=` and leftover rescan clears redesign-class rows.
- `nonsense-fails-closed` — no-op propose leaves leftovers; no silent PASS.
- `uncodable-excluded` — `MAX_EMAIL_LENGTH` is never an assist target.

## How to get to it (user POV)

- Isolated `each_item` tree + `pydantic-surface-hop.json`.
- Mechanical: `conduit apply` (expect incomplete + Human checks).
- Assist: `conduit apply --assist-redesign` when an LLM is configured, or run the prove helper (stub proposer).

## Driving it with conduit CLI

Preconditions:

- `doctor.py` PASS.
- Isolated one-file `each_item` consumer with `pydantic==2.0.0`.

- **Honesty without flag.** Apply without `--assist-redesign`. Exit 1; stdout includes `Human checks` and `each_item`; file still has `each_item=True`.
- **Stub assist (offline proof).** Run `python .cursor/skills/verify-conduit/helpers/prove_assist_redesign.py`. Expect `RESULT.txt` PASS; post file has no `each_item=True`; redesign leftovers empty.
- **CLI assist (LLM).** When a provider is configured: `python -m conduit.main apply --path <scratch> --packet examples/sample-packet/pydantic-surface-hop.json --assist-redesign`. Leftover rescan must clear redesign rows or remain dirty with checklist — never invent `MAX_EMAIL_LENGTH`.
- **Proof.** Save apply transcripts, checklist block, and before/after `each_item.py` under `evidence/assist-redesign-<UTC>/`.

## Gotchas

- `--assist-redesign` without LLM prints a yellow warning and keeps leftovers (honesty).
- Dropping `each_item=` is a minimal stub redesign for offline proof; production LLM may rewrite to item-level validators.
- Uncodable side_effects remain on the human checklist even when redesign leftovers clear.
