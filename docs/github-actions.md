# GitHub Actions

Conduit ships workflows you can copy into consumer repos, plus a composite action.

## Workflows in this repo

| File | Trigger | Purpose |
|------|---------|---------|
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Push / PR | Unit tests + dry-run apply sanity |
| [`.github/workflows/conduit-watch-demo.yml`](../.github/workflows/conduit-watch-demo.yml) | Push / PR | Watch dirty-fail + clean-pass on ephemeral fixtures |
| [`.github/workflows/conduit-dependabot.yml`](../.github/workflows/conduit-dependabot.yml) | PR touching manifests/lockfiles | Older `conduit run --base-ref` example. Not the outside-service product |
| [`.github/workflows/conduit-nightly.yml`](../.github/workflows/conduit-nightly.yml) | Cron `0 0 * * *` | Nightly detect/migrate pass |

## Watch

`conduit watch` without `--packet` diffs outside services (PyPI wheels, botocore models, webhook schemas). Release notes are hints. It does not bump lockfiles or announce ordinary library versions.

`conduit watch --packet` is a separate read-only leftover gate. It fails when the package pin has reached the packet `to_version` but call sites still hit packet `old_callee` leftovers. That check belongs to apply. It does not watch another repo.

This repo proves it with ephemeral fixtures in [`.github/workflows/conduit-watch-demo.yml`](../.github/workflows/conduit-watch-demo.yml) (no `OPENAI_API_KEY`, committed demo tree untouched).

Consumer copy pattern:

```yaml
- uses: conduit-oss/conduit/conduit@main
  with:
    mode: watch
    path: .
    packet: ./packets/my-hop.json
```

Or call the CLI directly:

```bash
conduit watch --path . --packet ./packets/my-hop.json
```

## Not a Dependabot intercept

Conduit does not open lockfile bump PRs, scan for CVEs, or send new-version notices. The outside-service path does not watch internal repos.

[`.github/workflows/conduit-dependabot.yml`](../.github/workflows/conduit-dependabot.yml) is an older example of `conduit run --base-ref` on a pull request that already touches a manifest. `--base-ref` limits that packet pipeline to the lockfile diff on the branch. It is not the map / watch / impact product, and it does not turn Conduit into Dependabot.

## Nightly

Runs `conduit run` on a schedule. In *this* monorepo it also exercises the demo consumer with the sample packet. Consumer apps should point `--path` at their application root and supply packets / modules they care about.

## Composite action

[`conduit/action.yml`](../conduit/action.yml) installs Conduit with `[llm,langs,dev]` and runs `conduit run` (default) or `conduit watch` when `mode: watch`.

Inputs:

- `path`, `base-ref`, `package`, `packet` (file path **or** package name), `skip-pr`, `mode` (`run` or `watch`), `python-version`

Pass-through env for LLMs:

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
- `CONDUIT_LLM_PROVIDER`, `CONDUIT_LLM_MODEL`, `CONDUIT_LLM_API_KEY`, `CONDUIT_LLM_BASE_URL`
- `GH_TOKEN` / `GITHUB_TOKEN`

### Example consumer usage

```yaml
jobs:
  migrate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: conduit-oss/conduit/conduit@main
        with:
          path: .
          base-ref: origin/main
          skip-pr: "false"
        env:
          CONDUIT_LLM_PROVIDER: ollama   # or openai / anthropic / custom
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

(Pin to a release tag once you publish actions versions.)

## Permissions

For PR creation you typically need:

```yaml
permissions:
  contents: write
  pull-requests: write
```

## Related docs

- [Pull requests](pull-requests.md)
- [Detection](detection.md)
- [LLM configuration](llm.md)
