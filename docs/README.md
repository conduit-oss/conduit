# Conduit documentation

Conduit maps the outside services a Python repo talks to and shows the lines that break when one of those services changes. The commands are `map`, `watch`, `impact`, and `service`. `conduit apply` is deferred. Scope is external API SDKs and REST, databases, cloud SDKs (S3 through botocore models), and incoming webhooks such as Stripe.

This is not Dependabot territory: no lockfile bumps, no CVE alerts, and no new-version notices for ordinary libraries. Change data comes from diffing published wheels, botocore models, and webhook schemas. GitHub releases and changelogs are extra hints. There is no hand-written change list and no internal repo-to-repo watch.

Python only today. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next.

Packet authoring stays in this repo for maintainers. Detect stays Advanced. Start with the [root README](../README.md).

## Consumer

Map the repo, then watch or impact one outside service. Watch uses no LLM. `conduit apply` is deferred; `conduit run` still rewrites a tree from a frozen packet.

| Doc | Covers |
|-----|--------|
| [Getting started](getting-started.md) | Install, map, watch, impact, hosted service skeleton |
| [GitHub Actions](github-actions.md) | Leftover Watch gate, nightly, composite action |
| [CLI reference](cli-reference.md) | Every command and flag |
| [Outside-in mode](outside-in.md) | Call graph through wrappers, aliases, and re-exports; wheel and schema diffs; hosted watch |
| [Migration backtest](backtest.md) | Six merged SDK migrations: recall, precision, and edit coverage |
| [Codemods](codemods.md) | Packet rule types. Map, watch, and impact are Python; `conduit run` still has extra language engines |
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
