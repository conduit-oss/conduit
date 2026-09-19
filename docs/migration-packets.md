# Migration packets

A **Migration Packet** is a JSON document (`conduit-packet.json`) that lists deterministic codemod rules for one package version jump.

Schema: [`schema/conduit-packet.schema.json`](../schema/conduit-packet.schema.json)

## Vendor vs consumer

| Role | Typical action |
|------|----------------|
| Vendor / maintainer (producer) | `conduit packet new` → `packet test` → `packet publish` into the catalog. Hop scaffolding also via `packet init` / `synthesize` / `from-detect`. |
| Consumer (client) | Pull from the catalog (`CONDUIT_PACKET_CATALOG_BASE` or a raw URL), then `conduit packet test` / `conduit run --packet …` / `conduit watch` |

Producer flow is **publish, then consumer pull**. `packet publish` writes catalog JSON only. It does not open PRs into consumer repos.

## Minimal shape

```json
{
  "packet_id": "openai-0.28.1-1.40.0",
  "package": "openai",
  "ecosystem": "pypi",
  "from_version": "0.28.1",
  "to_version": "1.40.0",
  "sources": [ { "url": "…", "kind": "docs" } ],
  "notes": "optional",
  "side_effects": [
    {"kind": "webhook", "detail": "Receivers must accept max_completion_tokens in the payload."}
  ],
  "ignore": {
    "globs": ["**/policy.py"],
    "paths": [],
    "patterns": []
  },
  "rules": [ /* see Codemods */ ]
}
```

Optional `ignore` protects migration-contract files/patterns from self-correct heuristics and LLMs (also auto-merged with `.conduit/ignore.json` and discovered `LEGACY_`/`FORBIDDEN_` fixtures). See [Testing & self-correction](testing-and-self-correct.md).

Optional `side_effects` is a human checklist (`webhook`, `database`, `config`, `other`) for ripples Conduit cannot apply (payload receivers, stored field names). It is not executed. Items appear under **Double-check** in the run summary and PR body.

`ecosystem` is one of: `pypi`, `npm`, `go`, `maven`, `other`.

### Version fields

| Field | Meaning |
|-------|---------|
| `from_version` | Version the **consumer currently uses** (or the pre-bump side of a lockfile jump). Author-time `packet new` writes `*`. |
| `to_version` | Migration **target** version |
| `packet_id` | Conventionally `{package}-{from_version}-{to_version}` (link-authored packets use `{package}-{ecosystem}-{version}`) |

These top-level versions also drive **export delta** (downloading both package versions to compare public APIs). Rule-level `DEPENDENCY_BUMP.from_version` / `to_version` can still describe the pin rewrite independently.

Catalog snapshots use `from_version` `0` as a **floor**, not a PyPI/npm release. On `conduit run` / `apply` / `verify`, Conduit copies the consumer pin for the **packet ecosystem** onto that floor in memory (the published JSON is not rewritten) so export-delta and leftover pin tokens use the real pin. A pypi packet binds `requirements.txt` / `pyproject.toml`. An npm packet binds `package.json`. The same package name in both ecosystems does not share a pin. npm `openai@3.3.0` is never stamped onto a pypi hop.

`DEPENDENCY_BUMP` rewrites nested `requirements.txt` / `constraints.txt` / `*requirements*.txt` and nested `package.json` (skipping `vendor/`, venvs, `node_modules`, `.conduit`). Root `pyproject.toml` / `go.mod` / Maven / Gradle stay root-only.

Before apply, catalog rules are **scoped to source-packet usage**. Replace chains collapse (`A→B` + `B→C` becomes `A→C`) and unused model/callee string rules are dropped. `DEPENDENCY_*` always stays.

### Coverage (source vs packet)

Coverage scores each client `model_id` / API token against **migrate-from** rules only:

| Tag | Meaning |
|-----|---------|
| `WILL MIGRATE` | Packet has a rule that changes this (e.g. `gpt-4-0613` → `gpt-4o`) |
| `KEEP` | This id is already a replacement target; leave it |
| `NO RULE` | Used in this repo; packet has no replace-from rule (may still be current, e.g. `gpt-4o-mini`) |
| `UNMAPPED` | Helper name or short path, not a known `/v1` route. Not a gap |

## Where packets come from (`ensure_packet`)

Resolution order in `conduit run`:

1. **`--packet` file path.** If the value names an existing file, load that JSON.
2. **`--packet` package name.** e.g. `--packet openai` (same idea as `--package openai`): synthesize/cache for that package.
3. **Cache.** `.conduit/packets/{package}-{from}-{to}.json` (skip with `--refresh-packet`).
4. **Synthesize from detect signals.** Fold `suggested_rules` into a packet.
5. **OpenAI fixture fallback.** Only when `--demo` is set and package is `openai` with empty rules (warns).

Cached after synthesis so the next run is instant. Explicit packet **files** are never overwritten by version rewriting. Use `--refresh-packet` when live detect has new signals and you want to rebuild the cached packet for the same version pair. **Required after detect/normalize or LLM-evidence changes**, otherwise `conduit run` may keep applying a stale cached packet.

When an LLM is configured (not `--demo`), synthesis also runs **evidence-grounded enrichment**: the [`llms.txt` doc router](../conduit/src/conduit/packet/doc_router.py) selects official pages, Conduit pre-fetches migration doc excerpts, code examples, and OpenAPI path schemas into the enrich prompt, then the model may `fetch_url` for gaps before emitting rules. At `conduit run`, published packets may be **doc-augmented** for client-specific coverage gaps (see [`doc_augment`](../conduit/src/conduit/packet/doc_augment.py)). See [LLM configuration](llm.md).

### How `from_version` / `to_version` are chosen (synthesis)

When not loading a packet file:

1. **Signal pair.** First detect signal for the package with both `from_version` and `to_version` (typically a lockfile/manifest jump).
2. Else **`from_version` from manifests.** Current pin via `read_installed()` (`requirements.txt`, `pyproject.toml`, `package.json`, `go.mod`).
3. Else **`to_version` from signals** or from a `DEPENDENCY_BUMP` rule on those signals.
4. Else placeholders `0.0.0` / `1.0.0` (Conduit prints a **warning**).

After synthesis (or openai fixture load), the packet’s top-level versions and `packet_id` are aligned to the resolved pair.

```bash
# Package name. Works for any package that detect can target.
conduit run --path . --packet openai -v

# Explicit packet file
conduit run --path . --packet ./packets/openai-0.28.1-1.40.0.json
```

With `-v`, Conduit prints version sources (`manifest`, `signal`, `rule`, `fixture`, `placeholder`, `file`, `cache`).

## Authoring CLI

### Author a packet from links (`packet new`)

Humans answer prompts or flags only. No hand-edited rules required. Point Conduit at migrate guides, changelogs, or docs. When an LLM is configured it fills `rules` (and `side_effects` for multi-statement gaps). Without an LLM you still get a schema-valid packet with a dependency hop and recorded `sources`.

```bash
# Interactive (TTY): package → ecosystem → version → source URLs (blank to finish)
conduit packet new

# Flags (non-interactive / CI)
conduit packet new \
  --package google-genai \
  --ecosystem pypi \
  --version 1.0.0 \
  --source-url https://googleapis.github.io/python-genai/ \
  --source-url https://github.com/googleapis/python-genai/blob/main/CHANGELOG.md

# Skip LLM even when configured
conduit packet new --package google-genai --version 1.0.0 \
  --source-url https://example.com/migrate --scaffold-only
```

Flags on `packet new`: `--package`, `--ecosystem`, `--version` / `--to`, `--source-url` (repeatable), `--out`, `--no-enrich`, `--scaffold-only`.

Default output: `packets/{package}-{ecosystem}-{version}.json` (e.g. `packets/google-genai-pypi-1.0.0.json`).
`packet_id` matches that slug. Top-level `from_version` is `*` (any consumer pin; resolved at apply/run). `to_version` is the target you picked.

Try it without a demo consumer:

```bash
conduit packet test --packet ./packets/google-genai-pypi-1.0.0.json
conduit packet test --packet ./packets/google-genai-pypi-1.0.0.json --path /tmp/my-app
conduit apply --packet ./packets/google-genai-pypi-1.0.0.json --path /tmp/my-app --dry-run
conduit run --path /tmp/my-app --packet ./packets/google-genai-pypi-1.0.0.json --skip-pr
```

Example: migrating consumers from `google-generativeai` to `google-genai`. Author a packet for the **successor** package (`google-genai`) with the migrate-guide URL(s). Enrichment emits import/call rewrites when grounded. Uncodemodable multi-statement gaps land in `side_effects` (checklist only. Not applied by codemods).

### Hop scaffolding (`init`, `synthesize`, `from-detect`)

Use these when you need an explicit `from` → `to` hop, a changelog/docs fill, or a detect-module snapshot. They are **not** `packet new`.

```bash
# Empty scaffold directory (explicit from/to)
conduit packet init \
  --package openai --from 0.28.0 --to 1.0.0 \
  --ecosystem pypi --out ./my-packet

# Fill rules from changelog/docs (uses LLM if configured; otherwise keeps seed/empty rules)
conduit packet synthesize \
  --package openai --from 0.28.0 --to 1.0.0 \
  --changelog ./CHANGELOG.md --docs ./MIGRATION.md \
  --out ./my-packet/conduit-packet.json

# Catalog snapshots from detect (no consumer repo). Scan picks latest per ecosystem.
conduit packet from-detect --module openai --out-dir ./packets
# packets/openai-pypi-<latest>.json
# packets/openai-npm-<latest>.json

# Show hop-chain rule delta vs previous snapshot
conduit packet diff-rules ./packets/openai-pypi-1.40.0.json \
  --previous ./packets/openai-pypi-1.0.0.json

conduit packet validate ./my-packet/conduit-packet.json
conduit packet show ./my-packet/conduit-packet.json

# Merge leftover-token / post-apply rules into a packet after a consumer run
conduit packet export-post-rules --path ./examples/demo-consumer \
  --packet ./packets/openai-pypi-1.0.0.json
```

`from-detect` freezes **what the scan sees now**. The next new latest is a new file whose `from_version` is the last file’s `to_version` for that package+ecosystem. It is not a git-history replay. `--enrich` optionally adds LLM rules. Default is scrape-only. OpenAI snapshots derive rules from detect signals (OpenAPI param rename/removal, endpoint path pairs → usage-scoped `AST_CALL_REWRITE`, deprecations). Not from a static Python seed list.

Clients apply **one hop** (file or URL). They do not scrape OpenAI to author rules:

```bash
conduit run --path . --packet ./packets/openai-pypi-1.109.1.json
conduit run --path . --packet https://example.com/packets/openai-pypi-1.109.1.json
```

Give a Python client the **pypi** hops with `to_version` greater than their pin, in order. Node clients get the **npm** chain. There is no `--packet-dir` chain runner yet. The wrong hop still rewrites the pin and can skip earlier delta rules.

**Apply order.** `conduit run` / `conduit apply` runs two stages on the scoped packet. **SDK** first (dependency bumps + AST call/import/param rules on code files), then **REST** (EXACT/REGEX path and model literals, KEY_RENAME in configs). REST path strings such as `/v1/fine-tunes` are never applied as Python callees. Invalid `AST_CALL_REWRITE` rules are skipped at apply time.

## Publish into a catalog

```bash
conduit packet publish \
  --packet ./examples/sample-packet/conduit-packet.json \
  --catalog /path/to/conduit-packets
# or --catalog git@github.com:conduit-oss/conduit-packets.git
```

Layout: `by-package/<package>/<ecosystem>/<packet_id>.json`. When `packet_id` lacks `-<ecosystem>-` (for example `openai-0.28.1-1.0.0`), publish also writes a root `<packet_id>.json` so slug fetch via `CONDUIT_PACKET_CATALOG_BASE` can resolve without the eco token. While the catalog is private, or the slug cannot be parsed, prefer a full raw URL for `--packet`.

Optional `--notice-url` POSTs metadata after write. `--no-commit` writes files only and prints git commands.

## Rule types (summary)

| Type | Purpose |
|------|---------|
| `EXACT_STRING_REPLACE` | Literal find/replace |
| `REGEX_REPLACE` | Regex replace |
| `AST_PARAM_RENAME` | Rename kwarg / object key / builder method / struct key near a call |
| `AST_PARAM_DROP` | Omit a kwarg near a matching call (optional literal `values`) |
| `AST_IMPORT_REWRITE` | Rewrite import module path (Python, JS/TS, Java, Go) |
| `AST_ATTR_RENAME` | Rename attribute / member chain |
| `AST_CALL_REWRITE` | Rewrite call callee path |
| `KEY_RENAME` | Rename quoted dict/JSON/YAML keys and `.env` prefixes (config files included even if import-pruned) |
| `DEPENDENCY_BUMP` | Bump version in pip/npm/go.mod/Maven/Gradle manifests |
| `DEPENDENCY_ADD` | Add a companion pin (formatted per ecosystem; optional `scope`) |
| `DEPENDENCY_REMOVE` | Remove a pin (optional `scope`) |

Optional `reason` on any rule explains why it was chosen (endpoint-compat checks, deprecation docs, etc.). PR bodies render these under **Rationale**, with packet `notes` and full `sources`.

Full field docs: [Codemods](codemods.md).

## Example (shipped sample)

See [`examples/sample-packet/conduit-packet.json`](../examples/sample-packet/conduit-packet.json). Model string replace + `max_tokens` → `max_completion_tokens` + dependency bump.

## Related docs

- [Codemods](codemods.md)
- [Pruning & export delta](pruning-and-export-delta.md)
- [LLM configuration](llm.md) (for `packet synthesize`)
- [CLI reference](cli-reference.md)
