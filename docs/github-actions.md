# GitHub Actions

Conduit ships workflows you can copy into consumer repos, plus a composite action.

## Workflows in this repo

| File | Trigger | Purpose |
|------|---------|---------|
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Push / PR | Unit tests + dry-run apply sanity |
| [`.github/workflows/conduit-dependabot.yml`](../.github/workflows/conduit-dependabot.yml) | PR touching manifests/lockfiles | Intercept Dependabot/Renovate bumps |
| [`.github/workflows/conduit-nightly.yml`](../.github/workflows/conduit-nightly.yml) | Cron `0 0 * * *` | Nightly detect/migrate pass |

## Dependabot / Renovate intercept

Idea: when a bot opens a lockfile PR across a breaking boundary, Conduit runs on that branch’s diff.

```yaml
# simplified from conduit-dependabot.yml
- run: |
    conduit run \
      --path . \
      --base-ref "origin/${{ github.base_ref }}" \
      --skip-pr
```

`--base-ref` makes Tier 1 lockfile isolation see exactly what changed on the PR.

Actor filter (bot or `conduit` label) avoids running on unrelated human PRs.

## Nightly

Runs `conduit run` on a schedule. In *this* monorepo it also exercises the demo consumer with the sample packet. Consumer apps should point `--path` at their application root and supply packets / modules they care about.

## Composite action

[`conduit/action.yml`](../conduit/action.yml) installs Conduit with `[llm,langs,dev]` and runs `conduit run` with inputs:

- `path`, `base-ref`, `package`, `packet` (file path **or** package name), `skip-pr`, `python-version`

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
