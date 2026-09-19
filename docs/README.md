# Conduit documentation

Two audiences share this repo. Consumers apply published packets. Producers author and publish them. Detect stays Advanced.

Start with the [root README](../README.md) for both paths in one screen.

## Consumer

Load a packet, apply or run, then gate leftovers with Watch.

| Doc | Covers |
|-----|--------|
| [Getting started](getting-started.md) | Install, packet apply, Watch gate, first real-repo run |
| [GitHub Actions](github-actions.md) | Watch CI gate, Dependabot intercept, nightly, composite action |
| [CLI reference](cli-reference.md) | Every command and flag |
| [Codemods](codemods.md) | Rule types and pluggable language engines (Python, JS/TS, Java, Go) |
| [LLM configuration](llm.md) | OpenAI, Anthropic, Ollama, custom local |
| [Testing & self-correction](testing-and-self-correct.md) | Test runners, smoke-test gen, retry loop |
| [Pull requests](pull-requests.md) | Branching, `gh`, PR body |
| [Architecture](architecture.md) | End-to-end pipeline and design principles |

## Producer

Author Migration Packets and publish them into a catalog.

| Doc | Covers |
|-----|--------|
| [Migration packets](migration-packets.md) | Schema, `packet new` / `test` / `publish`, hop scaffolding, version resolution |
| [CLI reference](cli-reference.md#conduit-packet) | Packet subcommands and flags |
| [Codemods](codemods.md) | Rule types you put in a packet |
| [LLM configuration](llm.md) | Enrichment for `packet new` and `packet synthesize` |

## Advanced

Detect modules and lockfile-first detection remain for maintainers who author packets from vendor signals. They are not the public hero path. See the leave-public-OSS note in [Detect modules](detect-modules.md).

| Doc | Covers |
|-----|--------|
| [Detection](detection.md) | Lockfile diffs, manifests (`read_installed`), version jumps |
| [Pruning & export delta](pruning-and-export-delta.md) | Import filter + package API comparison |
| [Detect modules](detect-modules.md) | Plugin API, OpenAI module, scaffolding (`conduit module new`) |
