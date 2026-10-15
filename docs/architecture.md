# Architecture

Conduit is a **self-hosted migration engine**. It does not rely on a central packet registry. When you run `conduit run`, it builds (or loads) a Migration Packet and applies deterministic transforms in the consumer repo.

## Design principles

1. **Deterministic first** — production code changes come from packet rules (AST/string), not free-form LLM rewrites.
2. **Scoped work** — lockfile isolation → import prune → export-delta prune so large monorepos stay cheap.
3. **Verify against the consumer’s own tests** — pytest / `npm test` / `go test`.
4. **LLM as a backup** — optional for packet synthesis, failed-test repair, and missing-test generation.
5. **Pluggable detection** — vendor **detect modules** emit signals that can seed packet rules.
6. **Python first** — `map`, `watch`, and `impact` read Python only. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next. Packet apply still dispatches AST rules through language engines already in the tree (libcst for Python, tree-sitter for JS/TS, Java, and Go). Those engines are not the outside-service path.

## Pipeline

```text
┌─────────────────┐
│ 1. Detect       │  lockfile/manifest git diff + detect modules
└────────┬────────┘
         ▼
┌─────────────────┐
│ 2. Prune        │  import-string filter (drop unrelated files)
└────────┬────────┘
         ▼
┌─────────────────┐
│ 3. Export delta │  compare public APIs old vs new package version
└────────┬────────┘
         ▼
┌─────────────────┐
│ 4. Packet       │  --packet file | --packet <pkg name> | cache | synth
└────────┬────────┘
         ▼
┌─────────────────┐
│ 5. Apply        │  AST / string / dependency codemods
└────────┬────────┘
         ▼
┌─────────────────┐
│ 6. Verify       │  ensure tests → run suite → self-correct (≤5)
└────────┬────────┘
         ▼
┌─────────────────┐
│ 7. PR           │  branch conduit/upgrade-{pkg}-{ver} → gh pr create
└─────────────────┘
```

After packet apply, `conduit run` still calls `apply_openai_client_chain` for openai client-shape polish (`OpenAI()` + `client.*`, stale pre-1.0 guard strip). That step is openai-only. It is not a generic apply path for other packages. Packet rules alone are not the full openai kill bar. See [OpenAI client chain](migration-packets.md#openai-client-chain).

## Outside-in

This is the product path. Scope is outside services only: external API SDKs and REST, databases, cloud SDKs such as S3 via botocore models, and incoming webhooks such as Stripe. It is not Dependabot territory (no lockfile bumps, no CVE alerts, no new-version notices) and it does not watch internal repos.

Import pruning sees files that mention the SDK. A helper that wraps `openai.ChatCompletion.create` hides every caller that only imports the helper. `conduit map` inventories catalogued services (SDK imports, client constructors, HTTP calls to known hosts) and walks wrappers, aliases, re-exports, `functools.partial`, literal `getattr`, and lambdas on the static Python call graph. Imports inside functions are included. `conduit watch` and `conduit impact --dep X [--from V] [--to V]` intersect that graph with a diff of old and new PyPI wheels (removed, renamed, and changed-signature functions, and return types), botocore service models, webhook schemas, and a versioned OpenAPI pair when one is recorded. GitHub releases and changelogs are extra hints. There is no hand-written change list. A version is taken from `--from`, an exact pin, a lockfile, the installed distribution, or a requirement range resolved on PyPI. If none of those exist, the note says `not compared` and why. `conduit apply` is deferred. `conduit service` stores one map per customer, runs the same watch, and writes a note of the lines that break. See [Outside-in mode](outside-in.md).

Packet `from_version` prefers lockfile jump signals, else the version declared in manifests (`read_installed`). `to_version` comes from signals or `DEPENDENCY_BUMP` rules. See [Migration packets](migration-packets.md).

## Tiered scoping (why it stays fast)

| Tier | Mechanism | Effect |
|------|-----------|--------|
| 1 | Lockfile/manifest git diff | Only upgraded packages matter (e.g. 1 of 50) |
| 2 | Import grep | Only files that import those packages |
| 3 | Export delta | Prefer files mentioning changed/removed symbols |
| 4 | Hard excludes | Skip `node_modules/`, `.git/`, `dist/`, `vendor/`, etc. |

AST work runs only on the surviving file set.

## Core packages (code map)

| Path | Role |
|------|------|
| `conduit/detect/` | Orchestrator, lockfile diff, module plugins |
| `conduit/outside_in/` | External-service inventory and Python call graph (outside-in) |
| `conduit/impact/` | Wheel, botocore, and webhook-schema diffs, intersected with the map |
| `conduit/service/` | Hosted skeleton: stored maps, scheduled watch, local fix PR |
| `conduit/prune/` | Import pre-filter |
| `conduit/export_delta/` | Fetch + diff package exports |
| `conduit/packet/` | Cache, validate, synthesize |
| `conduit/patcher/` | Apply packet rules. Python is the outside-service path; extra engines remain for frozen packets |
| `conduit/patcher/languages/` | Per-language engines, precise edits, optional formatters |
| `conduit/llm/` | Model-agnostic chat client |
| `conduit/test_runner.py` / `test_gen.py` / `self_correct.py` | Verify loop |
| `conduit/pr_generator.py` | Branch + PR |

## What is *not* in the hot path

- A hosted “Central Migration Registry” — packets live in-repo (`.conduit/packets/`), are passed as `--packet ./file.json`, or are synthesized from `--packet <package-name>` / detect signals.
- LLM rewriting every file on every run — apply is rule-based unless tests fail and an LLM is configured.

## Related docs

- [Detection](detection.md)
- [Pruning & export delta](pruning-and-export-delta.md)
- [Codemods](codemods.md)
- [Testing & self-correction](testing-and-self-correct.md)
