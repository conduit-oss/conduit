# Conduit documentation

Deep dives for every major part of the system. Start with the [root README](../README.md) for the consumer path (**packets → apply → Watch**), then use these pages when you need detail.

## Consumer path

| Doc | Covers |
|-----|--------|
| [Getting started](getting-started.md) | Install, packet apply, Watch gate, first real-repo run |
| [Migration packets](migration-packets.md) | Schema, `--packet` name/file, version resolution, cache, synthesis |
| [GitHub Actions](github-actions.md) | Watch CI gate, Dependabot intercept, nightly, composite action |
| [CLI reference](cli-reference.md) | Every command and flag |
| [Codemods](codemods.md) | Rule types and pluggable language engines (Python, JS/TS, Java, Go) |
| [LLM configuration](llm.md) | OpenAI, Anthropic, Ollama, custom local |
| [Testing & self-correction](testing-and-self-correct.md) | Test runners, smoke-test gen, retry loop |
| [Pull requests](pull-requests.md) | Branching, `gh`, PR body |
| [Architecture](architecture.md) | End-to-end pipeline and design principles |

## Advanced / private-factory adjacent

Detect modules and lockfile-first detection remain in this repo for maintainers who author packets from vendor signals. They are not the public hero path. See the leave-public-OSS note in [Detect modules](detect-modules.md).

| Doc | Covers |
|-----|--------|
| [Detection](detection.md) | Lockfile diffs, manifests (`read_installed`), version jumps |
| [Pruning & export delta](pruning-and-export-delta.md) | Import filter + package API comparison |
| [Detect modules](detect-modules.md) | Plugin API, OpenAI module, scaffolding (`conduit module new`) |
