# Pull requests

After a successful verify, Conduit opens a migration PR (unless `--skip-pr`).

Implementation: [`conduit/src/conduit/pr_generator.py`](../conduit/src/conduit/pr_generator.py).

## Branch naming

```text
conduit/upgrade-{package}-{to_version}
```

Example: `conduit/upgrade-openai-1.0.0`

Created with `git checkout -B` (reset branch if it already exists).

## What gets committed

- All files modified by apply / test-gen / self-correct
- Commit message derived from the packet (package + version jump)

## PR contents

- **Title** — migration summary from packet metadata  
- **Body** — packet id, rules applied, test runner result, detect signal summary (when available)

Created via:

```bash
gh pr create --title "…" --body "…"
```

Requires:

- Repo is a git work tree  
- `gh` on `PATH` and authenticated (`gh auth login`)  
- Network permissions to push (`--no-push` skips remote update)

## Flags

| Flag | Effect |
|------|--------|
| `--skip-pr` | Stop after green tests; leave changes in the worktree / local commits as implemented |
| `--no-push` | Prepare branch/commit but do not push / open remote PR |

Exact push vs commit behavior follows `open_pull_request(..., push=..., create_pr=...)`.

## CI pattern

`conduit run` can open a fix PR for a hop you already have. `conduit apply` is deferred. Conduit does not open Dependabot-style lockfile bumps, and it does not watch internal repos. An older workflow example passes `--base-ref` so `conduit run` sees a lockfile diff already on a branch, often with `--skip-pr`. That is not the outside-service product.

See [GitHub Actions](github-actions.md).

## Related docs

- [Testing & self-correction](testing-and-self-correct.md)
- [CLI reference](cli-reference.md)
