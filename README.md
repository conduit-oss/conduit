<p align="center">
  <img src="docs/assets/conduit-logo.svg" alt="Conduit" width="420"/>
</p>

<p align="center">
  <strong>Self-hosted CLI that updates your code when dependencies make breaking API changes.</strong>
</p>

<p align="center">
  Bump a package. Conduit finds the call sites, applies structural fixes, runs your tests, and opens a PR.
</p>

<p align="center">
  Deterministic AST engines for <strong>Python, JS/TS, Java, and Go</strong> — LLM only as backup.
</p>

```text
Detect upgrade  →  Prune files  →  Apply migration rules  →  Test / fix  →  Open PR
```

<p align="center">
  📚 <a href="docs/README.md"><strong>Full documentation</strong></a>
</p>

---

## Quick start

```bash
git clone https://github.com/conduit-oss/conduit.git
cd conduit
python -m venv .venv
source .venv/bin/activate          # Windows: .venv/Scripts/activate
python -m pip install -e "./conduit[llm,langs,dev]"

conduit run \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json \
  --demo \
  --skip-pr
```

The GitHub repo is **private** under `conduit-oss`; you need access to clone it.

`--demo` uses offline detect fixtures and does **not** require a consumer `OPENAI_API_KEY` for verify. Optional LLM self-correct still uses `CONDUIT_LLM_*` if configured. For apply-only (no tests), add `--skip-tests`.

This **really applies** the sample Migration Packet, runs the demo tests, and skips only PR creation so you can inspect the diff:

```bash
git -C examples/demo-consumer diff
```

On your own repo:

```bash
conduit run --path /path/to/your/repo --packet openai -v
# or an explicit packet file
conduit run --path /path/to/your/repo --packet ./my-packet/conduit-packet.json
```

`--packet` accepts a **package name** or a path to `conduit-packet.json`. Use `-v` for version-source and export-delta diagnostics.

More: [Getting started](docs/getting-started.md) · [CLI reference](docs/cli-reference.md)

---

## How it works

| Step | What happens |
|------|----------------|
| **Detect** | Lockfile/manifest git diffs + vendor detect modules |
| **Prune** | Keep files that import the upgraded package |
| **Export delta** | Compare public APIs between old/new versions |
| **Packet** | Load or build `conduit-packet.json` rules |
| **Apply** | Deterministic AST/string codemods for Python, JS/TS, Java, Go (no LLM required) |
| **Verify** | Native tests + optional LLM self-correct |
| **PR** | Branch `conduit/upgrade-{package}-{version}` |

Deep dive: [Architecture](docs/architecture.md)

---

## LLM (optional)

Packet **apply** never needs an LLM. Configure one only for synthesis, failed-test repair, or smoke-test generation.

| Provider | API key? |
|----------|----------|
| `openai` / `anthropic` | Yes |
| `ollama` / `custom` (local server) | **No** |

```bash
export CONDUIT_LLM_PROVIDER=ollama
export CONDUIT_LLM_MODEL=llama3.2
```

Details: [LLM configuration](docs/llm.md)

---

## Docs index

| Topic | Link |
|-------|------|
| Getting started | [docs/getting-started.md](docs/getting-started.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Detection | [docs/detection.md](docs/detection.md) |
| Pruning & export delta | [docs/pruning-and-export-delta.md](docs/pruning-and-export-delta.md) |
| Migration packets | [docs/migration-packets.md](docs/migration-packets.md) |
| Codemods | [docs/codemods.md](docs/codemods.md) |
| LLM | [docs/llm.md](docs/llm.md) |
| Testing & self-correct | [docs/testing-and-self-correct.md](docs/testing-and-self-correct.md) |
| Pull requests | [docs/pull-requests.md](docs/pull-requests.md) |
| Detect modules | [docs/detect-modules.md](docs/detect-modules.md) |
| GitHub Actions | [docs/github-actions.md](docs/github-actions.md) |
| CLI reference | [docs/cli-reference.md](docs/cli-reference.md) |

---

## License

MIT
