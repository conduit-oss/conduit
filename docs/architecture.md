# Architecture

Conduit is a **self-hosted migration engine**. It does not rely on a central packet registry. When you run `conduit run`, it builds (or loads) a Migration Packet and applies deterministic transforms in the consumer repo.

## Design principles

1. **Deterministic first** — production code changes come from packet rules (AST/string), not free-form LLM rewrites.
2. **Scoped work** — lockfile isolation → import prune → export-delta prune so large monorepos stay cheap.
3. **Verify against the consumer’s own tests** — pytest / `npm test` / `go test`.
4. **LLM as a backup** — optional for packet synthesis, failed-test repair, and missing-test generation.
5. **Pluggable detection** — vendor **detect modules** emit signals that can seed packet rules.
6. **Pluggable languages** — AST rules dispatch through language engines (libcst / tree-sitter) so deterministic fixes cover Python, JS/TS, Java, and Go before any LLM step.

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
| `conduit/prune/` | Import pre-filter |
| `conduit/export_delta/` | Fetch + diff package exports |
| `conduit/packet/` | Cache, validate, synthesize |
| `conduit/patcher/` | Apply rules via pluggable language engines (Python/JS/TS/Java/Go) |
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
