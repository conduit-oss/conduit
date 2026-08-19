# Migration packets

A **Migration Packet** is a JSON document (`conduit-packet.json`) that lists deterministic codemod rules for one package version jump.

Schema: [`schema/conduit-packet.schema.json`](../schema/conduit-packet.schema.json)

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

Optional `side_effects` is a human checklist (`webhook`, `database`, `config`, `other`) for ripples Conduit cannot apply (payload receivers, stored field names). It is not executed; items appear under **Double-check** in the run summary and PR body.

`ecosystem` is one of: `pypi`, `npm`, `go`, `maven`, `other`.

### Version fields

| Field | Meaning |
|-------|---------|
| `from_version` | Version the **consumer currently uses** (or the pre-bump side of a lockfile jump) |
| `to_version` | Migration **target** version |
| `packet_id` | Conventionally `{package}-{from_version}-{to_version}` |

These top-level versions also drive **export delta** (downloading both package versions to compare public APIs). Rule-level `DEPENDENCY_BUMP.from_version` / `to_version` can still describe the pin rewrite independently.

## Where packets come from (`ensure_packet`)

Resolution order in `conduit run`:

1. **`--packet` file path** — if the value names an existing file, load that JSON
2. **`--packet` package name** — e.g. `--packet openai` (same idea as `--package openai`): synthesize/cache for that package
3. **Cache** — `.conduit/packets/{package}-{from}-{to}.json` (skip with `--refresh-packet`)
4. **Synthesize from detect signals** — fold `suggested_rules` into a packet
5. **OpenAI fixture fallback** — only when `--demo` is set and package is `openai` with empty rules (warns)

Cached after synthesis so the next run is instant. Explicit packet **files** are never overwritten by version rewriting. Use `--refresh-packet` when live detect has new signals and you want to rebuild the cached packet for the same version pair — **required after detect/normalize or LLM-evidence changes**, otherwise `conduit run` may keep applying a stale cached packet.

When an LLM is configured (not `--demo`), synthesis also runs **evidence-grounded enrichment**: fetch module seed docs + web search, ask the model for additional `rules`, merge onto scrape rules. See [LLM configuration](llm.md).

### How `from_version` / `to_version` are chosen (synthesis)

When not loading a packet file:

1. **Signal pair** — first detect signal for the package with both `from_version` and `to_version` (typically a lockfile/manifest jump)
2. Else **`from_version` from manifests** — current pin via `read_installed()` (`requirements.txt`, `pyproject.toml`, `package.json`, `go.mod`)
3. Else **`to_version` from signals** or from a `DEPENDENCY_BUMP` rule on those signals
4. Else placeholders `0.0.0` / `1.0.0` (Conduit prints a **warning**)

After synthesis (or openai fixture load), the packet’s top-level versions and `packet_id` are aligned to the resolved pair.

```bash
# Package name — seamless for any package that detect can target
conduit run --path . --packet openai -v

# Explicit packet file
conduit run --path . --packet ./packets/openai-0.28.1-1.40.0.json
```

With `-v`, Conduit prints version sources (`manifest`, `signal`, `rule`, `fixture`, `placeholder`, `file`, `cache`).

## Authoring CLI

```bash
# Empty scaffold
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

conduit packet validate ./my-packet/conduit-packet.json
conduit packet show ./my-packet/conduit-packet.json
```

`from-detect` freezes **what the scan sees now**. The next new latest is a new file whose `from_version` is the last file’s `to_version` for that package+ecosystem. It is not a git-history replay. `--enrich` optionally adds LLM rules; default is scrape-only.

Clients apply **one hop** (file or URL). They do not scrape OpenAI to author rules:

```bash
conduit run --path . --packet ./packets/openai-pypi-1.109.1.json
conduit run --path . --packet https://example.com/packets/openai-pypi-1.109.1.json
```

Give a Python client the **pypi** hops with `to_version` greater than their pin, in order. Node clients get the **npm** chain. There is no `--packet-dir` chain runner yet; the wrong hop still rewrites the pin and can skip earlier delta rules.

## Rule types (summary)

| Type | Purpose |
|------|---------|
| `EXACT_STRING_REPLACE` | Literal find/replace |
| `REGEX_REPLACE` | Regex replace |
| `AST_PARAM_RENAME` | Rename kwarg / object key / builder method / struct key near a call |
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

See [`examples/sample-packet/conduit-packet.json`](../examples/sample-packet/conduit-packet.json) — model string replace + `max_tokens` → `max_completion_tokens` + dependency bump.

## Vendor vs consumer workflow

| Role | Typical action |
|------|----------------|
| Vendor / maintainer | `conduit packet from-detect --module openai --out-dir ./packets` (or init/synthesize); share JSON |
| Consumer | `conduit run --packet ./file.json` or `--packet https://…` (one hop); source packet from client inventory |

There is not yet a `conduit packet publish` registry command — share packets via git/HTTP for now.

## Related docs

- [Codemods](codemods.md)
- [Pruning & export delta](pruning-and-export-delta.md)
- [LLM configuration](llm.md) (for `packet synthesize`)
- [CLI reference](cli-reference.md)
