<p align="center">
  <img src="docs/assets/conduit-logo.svg" alt="Conduit" width="420"/>
</p>

<p align="center">
  <strong>Self-hosted CLI that applies Migration Packets when dependencies make breaking API changes.</strong>
</p>

<p align="center">
  Author a packet from docs links, test it, then apply structural fixes and open a PR.
</p>

<p align="center">
  Deterministic AST engines for <strong>Python, JS/TS, Java, and Go</strong> — LLM only as backup.
</p>

```text
packet new  →  packet test  →  apply / run  →  verify  →  PR
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

# 1) Author a packet from migrate-guide / changelog URLs
conduit packet new \
  --package google-genai \
  --ecosystem pypi \
  --version 1.0.0 \
  --source-url https://ai.google.dev/gemini-api/docs/migrate \
  --scaffold-only

# 2) Validate + summarize (no consumer repo required)
conduit packet test --packet ./packets/google-genai-pypi-1.0.0.json

# 3) Apply a known packet to a consumer (demo)
conduit run \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json \
  --demo \
  --skip-pr
```

The GitHub repo is **private** under `conduit-oss`; you need access to clone it.

`--scaffold-only` writes a schema-valid dependency hop + sources (no invented AST rules). Drop it when an LLM is configured to enrich rules from the fetched URLs. `--demo` on `run` uses offline detect fixtures and does **not** require a consumer `OPENAI_API_KEY` for verify.

On your own repo:

```bash
conduit run --path /path/to/your/repo --packet ./packets/my-packet.json -v
# package name still works when a detect module / cache can synthesize:
# conduit run --path /path/to/your/repo --packet openai -v
```

`--packet` accepts a **file path**, an **https URL**, or a **package name**. Use `-v` for version-source and export-delta diagnostics.

More: [Getting started](docs/getting-started.md) · [Migration packets](docs/migration-packets.md) · [CLI reference](docs/cli-reference.md)

---

## How it works

| Step | What happens |
|------|----------------|
| **Author** | `conduit packet new` from docs links → schema-valid Migration Packet |
| **Test** | `conduit packet test` validates / dry-runs without requiring a demo consumer |
| **Detect** (optional) | Lockfile/manifest + client usage scan; `--module openai` is the reference vendor module |
| **Apply** | Deterministic AST/string codemods from the packet (no LLM required) |
| **Verify** | Native tests + optional LLM self-correct |
| **PR** | Branch `conduit/upgrade-{package}-{version}` |

Vendor **detect modules** (`conduit module new`) are **advanced** tooling for maintainers who need OpenAI-style signal workers. The public hero path is packets, not module sprawl.

Deep dive: [Architecture](docs/architecture.md) · [Detect modules](docs/detect-modules.md)

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

Apache-2.0
