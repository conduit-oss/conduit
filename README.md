<p align="center">
  <img src="docs/assets/conduit-logo.svg" alt="Conduit" width="420"/>
</p>

<p align="center">
  <strong>Self-hosted CLI that updates your code when dependencies make breaking API changes.</strong>
</p>

<p align="center">
  Deterministic AST engines for <strong>Python, JS/TS, Java, and Go</strong>. LLM only as backup.
</p>

```text
Consumer (client)                    Producer (vendor / maintainer)
─────────────────                    ─────────────────────────────
load a packet                        conduit packet new
conduit apply / conduit run          conduit packet test
conduit watch                        conduit packet publish → catalog
```

<p align="center">
  <a href="docs/README.md"><strong>Full documentation</strong></a>
  · <a href="docs/migration-packets.md">Packets</a>
  · <a href="docs/github-actions.md">Watch in CI</a>
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
  --skip-tests \
  --skip-pr
```

The GitHub repo is **private** under `conduit-oss`. You need access to clone it.

`--demo` uses offline fixtures and does **not** require a consumer `OPENAI_API_KEY` for verify. `--skip-tests` proves the structural openai 0.28 `ChatCompletion.create` → 1.x kill-bar hop without running demo suite asserts that intentionally encode the legacy surface. Restore with `git -C examples/demo-consumer checkout -- .` when finished.

This **really applies** the sample Migration Packet and skips only PR creation so you can inspect the diff:

```bash
git -C examples/demo-consumer diff
```

Gate the same hop with Watch (read-only; no rewrites):

```bash
conduit watch \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json
```

On your own repo:

```bash
conduit run --path /path/to/your/repo --packet openai -v
# or an explicit packet file
conduit run --path /path/to/your/repo --packet ./my-packet/conduit-packet.json
```

`--packet` accepts a **package name** or a path to `conduit-packet.json`. Use `-v` for version-source and export-delta diagnostics.

More: [Getting started](docs/getting-started.md) · [CLI reference](docs/cli-reference.md) · [GitHub Actions / Watch](docs/github-actions.md)

---

## Produce a packet

For vendors and maintainers who publish rules into a catalog:

```bash
conduit packet new \
  --package google-genai --ecosystem pypi --version 1.0.0 \
  --source-url https://googleapis.github.io/python-genai/
conduit packet test --packet ./packets/google-genai-pypi-1.0.0.json
conduit packet publish \
  --packet ./packets/google-genai-pypi-1.0.0.json \
  --catalog /path/to/conduit-packets
```

Details: [Migration packets](docs/migration-packets.md)

---

## How it works

| Audience | Step | What happens |
|----------|------|----------------|
| Consumer | **Packet** | Load `conduit-packet.json` rules for one version hop |
| Consumer | **Apply / Run** | Deterministic AST/string codemods (no LLM required) |
| Consumer | **Watch** | Fail CI when the pin reached `to_version` but `old_callee` leftovers remain |
| Consumer | **Verify / PR** | Native tests, optional LLM self-correct, branch `conduit/upgrade-{package}-{version}` |
| Producer | **packet new** | Author from migrate-guide URLs; writes `packets/{pkg}-{eco}-{version}.json` |
| Producer | **packet test** | Validate and optional dry-run apply |
| Producer | **packet publish** | Place the hop in a catalog under `by-package/…` |

Detect modules stay Advanced / private-factory adjacent. See [Detect modules](docs/detect-modules.md).

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
| Getting started (consumer) | [docs/getting-started.md](docs/getting-started.md) |
| Produce a packet (producer) | [docs/migration-packets.md](docs/migration-packets.md#authoring-cli) |
| Migration packets | [docs/migration-packets.md](docs/migration-packets.md) |
| GitHub Actions / Watch | [docs/github-actions.md](docs/github-actions.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Codemods | [docs/codemods.md](docs/codemods.md) |
| LLM | [docs/llm.md](docs/llm.md) |
| Testing & self-correct | [docs/testing-and-self-correct.md](docs/testing-and-self-correct.md) |
| Pull requests | [docs/pull-requests.md](docs/pull-requests.md) |
| CLI reference | [docs/cli-reference.md](docs/cli-reference.md) |
| Advanced: detection | [docs/detection.md](docs/detection.md) |
| Advanced: pruning & export delta | [docs/pruning-and-export-delta.md](docs/pruning-and-export-delta.md) |
| Advanced: detect modules (factory) | [docs/detect-modules.md](docs/detect-modules.md) |

---

## License

Apache-2.0
