# Publish hop then consume from catalog

A producer publishes the pydantic **migration hop** (surface-authored JSON) and optional recipe sibling into a local catalog checkout; a consumer apply/watch uses the **catalog hop path**. Proves publish → consume without a live remote catalog or consumer PRs.

## Sub-features

- `publish-hop-recipe` — `packet publish --packet … --recipe … --catalog <tmp> --no-commit`.
- `catalog-layout` — files land at `by-package/pydantic/pypi/pydantic-1.10.13-2.0.0.json` and `.recipe.json`.
- `consume-from-catalog` — apply + watch an isolated fixture using the **catalog hop** path (not `examples/sample-packet/`).

## How to get to it (user POV)

- Create or init a disposable catalog git directory.
- `conduit packet publish --packet <hop> --recipe <recipe> --catalog <catalog> [--no-commit]`.
- `conduit apply` / `conduit watch` with `--packet` = catalog hop JSON.

## Driving it with conduit CLI

Preconditions:

- `doctor.py` PASS.
- Hop `examples/sample-packet/pydantic-surface-hop.json` and recipe `examples/reshape-recipes/pydantic-1.10.13-2.0.0.json`.
- Catalog path is empty or a fresh `git init` tree you own (not a shared remote checkout).

- **Init catalog.** Create `<catalog>`, `git init`, add a README, commit once (publish may commit unless `--no-commit`).
- **Publish.** Run `python -m conduit.main packet publish --packet examples/sample-packet/pydantic-surface-hop.json --recipe examples/reshape-recipes/pydantic-1.10.13-2.0.0.json --catalog <catalog> --no-commit`. Exit code is `0`.
- **Assert layout.** Confirm `<catalog>/by-package/pydantic/pypi/pydantic-1.10.13-2.0.0.json` and `…/pydantic-1.10.13-2.0.0.recipe.json` exist. (Name “surface hop” means authored from surface diff; it is a **migration** hop, not `packet_kind=surface` under `surfaces/`.)
- **Isolate consumer.** Copy `examples/pydantic-validator-fixture` to scratch; seed `pydantic==2.0.0`.
- **Consume.** Run `python -m conduit.main apply --path <scratch> --packet <catalog>/by-package/pydantic/pypi/pydantic-1.10.13-2.0.0.json`. Exit `0`. The `.recipe.json` sibling is **not** the apply input — rules are already merged into the hop.
- **Watch.** Run Watch with the **same catalog hop path**; expect `status=clean` after apply.
- **Proof.** Save publish stdout, listing of `by-package/pydantic/pypi/`, apply/Watch transcripts, and post-apply `model.py` under `evidence/catalog-publish-then-consume-<UTC>/`.

## Gotchas

- `packet publish` does **not** open consumer PRs. Catalog write only.
- `--no-commit` still writes files; verify by listing the catalog tree.
- Flat mirror `pydantic-1.10.13-2.0.0.json` at catalog root may also appear; prefer the `by-package/…` path for apply.
- Do not point `--catalog` at the Conduit source repo.
- True surface freezes (`packet_kind=surface`) publish under `surfaces/<version>.json` — a different layout than this hop.
