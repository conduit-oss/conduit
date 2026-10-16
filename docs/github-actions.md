# GitHub Actions

The workflows in this repo call the same commands as the README.

| File | What it does |
|------|----------------|
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Tests, then an offline map of `examples/wrapped-openai-fixture`. |
| [`.github/workflows/conduit-watch-demo.yml`](../.github/workflows/conduit-watch-demo.yml) | `conduit map` and `conduit watch` on recorded copies. |
| [`.github/workflows/conduit-dependabot.yml`](../.github/workflows/conduit-dependabot.yml) | `conduit map` when a pull request touches a dependency file. |
| [`.github/workflows/conduit-nightly.yml`](../.github/workflows/conduit-nightly.yml) | `conduit map` on a schedule. |

A minimal job:

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-python@v5
  with:
    python-version: "3.11"
- run: pip install -e ./conduit
- run: conduit map --path .
```

[`conduit/action.yml`](../conduit/action.yml) installs Conduit and maps the repo. Set `mode` to `watch` to watch instead. `path` is the folder to read. `python-version` defaults to 3.11.
