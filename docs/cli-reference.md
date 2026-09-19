# CLI reference

Entry point: `conduit` (Typer app in [`conduit/src/conduit/main.py`](../conduit/src/conduit/main.py)).

```bash
conduit --help
conduit <command> --help
```

## Global options

| Option | Description |
|--------|-------------|
| `--verbose` / `-v` | Extra diagnostics (packet version sources, export-delta resolve details, self-correct failure/fix details, etc.) |

Normal (non-`-v`) runs still print stage progress: detect signal counts, LLM enrich/repair banners, and `[llm] turn N/M` / tool names during agent loops. See [LLM configuration](llm.md#progress-logs-always-on).

**`conduit run` preflight** (before the pulse spinner): loads consumer `.env`, prints LLM provider status when configured, and yellow **Warning** lines for skipped steps (published packet, `--demo`, `--skip-*`, missing LLM). Verify still prompts for missing keys when tests need them.

Place before or after the subcommand:

```bash
conduit -v run --path . --packet openai
conduit run -v --path . --packet openai
```

---

## `conduit run`

Full pipeline: detect → prune → export delta → packet → apply → verify (oracle + tests + self-correct) → PR.

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root (must be a directory). On Windows prefer forward slashes or quotes if backslashes get stripped. |
| `--base-ref` | none | Git ref for lockfile diff (e.g. `origin/main`) |
| `--package` | auto | Package to migrate |
| `--module` | auto | Restrict detect modules (if omitted and a package name is known, Conduit uses a matching detect module when one exists) |
| `--packet` | cache/synth | Path to a packet JSON, **http(s) URL**, catalog **packet_id** (when `CONDUIT_PACKET_CATALOG_BASE` is set), or a package name (e.g. `openai`). File / URL / catalog slug **loads** the packet. A bare package name still synthesizes from detect. |
| `--demo` | false | Offline detect fixtures + openai demo packet fallback (default is **live** vendor sources) |
| `--refresh-packet` | false | Ignore `.conduit/packets` cache and re-synthesize from current detect signals (use after detect/normalize changes) |
| `--skip-tests` | false | Skip oracle generation + verify (apply only) |
| `--skip-pr` | false | Do not open a PR |
| `--no-push` | false | Do not push remote |
| `--skip-modules` | false | Lockfile detect only |
| `--skip-lockfile` | false | Modules only |
| `--skip-export-delta` | false | Skip export compare / prune |
| `--max-retries` | `5` | Self-correct attempts |
| `--verbose` / `-v` | false | Same as global verbose |

### `--packet` resolution

1. If the value is an **http(s) URL** → download JSON into `.conduit/packets/` (reuse the cached file unless `--refresh-packet`) and load it.
2. If the value is an **existing file** → load that packet JSON.
3. If `CONDUIT_PACKET_CATALOG_BASE` is set and the value looks like a **packet_id** (e.g. `example-sdk-pypi-1.0.0`) → fetch `{BASE}/by-package/{package}/{ecosystem}/{id}.json`. Hop ids without `-<ecosystem>-` (e.g. `openai-0.28.1-1.0.0`) use the root mirror `{BASE}/{id}.json` that `packet publish` writes. On miss, fall through.
4. Otherwise treat it as a **package name** → same idea as `--package <name>`: detect/synthesize a packet for that package.
5. If both `--packet <file-or-url>` and `--package` disagree on the package field, the **packet file** wins (with a warning).
6. If `--packet <name>` and `--package` disagree, **`--package`** wins (with a warning).

A **file or URL** skips vendor detect workers (no GitHub/OpenAI scrape). Conduit still scans the consumer repo for a **source packet** (imports / models) so prune and coverage work. Catalog floor `from_version` `0` is stamped from the client pin before export-delta/apply; unused catalog string rules are dropped. `--packet openai` (a name) still runs vendor detect and synthesizes.

Public catalog layout: `conduit-oss/conduit-packets` (`by-package/<pkg>/<ecosystem>/<packet_id>.json`).

### Version defaults and warnings

When synthesizing a packet (not loading a file), Conduit resolves `from_version` / `to_version` from detect signals, then manifests, then `DEPENDENCY_BUMP` rules. Placeholder versions (`0.0.0` / `1.0.0`) or the offline openai fixture trigger a **yellow warning**. Use `-v` to see sources (`manifest`, `signal`, `rule`, `fixture`, `placeholder`).

---

## `conduit detect`

Lockfile diff + vendor modules → print signals (or `--json`).

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--base-ref` | none | Diff base |
| `--module` | all | Single module name |
| `--skip-modules` | false | |
| `--skip-lockfile` | false | |
| `--majors-only` / `--all-bumps` | majors-only | Filter version jumps |
| `--demo` | false | Offline detect fixtures (default: live) |
| `--json` | false | Machine-readable signals |

Exit `0` if any signals, else `1`.

---

## `conduit watch`

Read-only CI gate. Scans for packet `old_callee` leftovers and compares the consumer pin to the packet hop.

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--packet` | required | Packet **file** path or **http(s) URL** |
| `--json` | false | Machine-readable verdict (`status`, `exit_code`, leftovers) |

Exit codes:

| Exit | When |
|------|------|
| `0` | Pin still at `from_version` (prints a **warn** if leftovers exist), or pin at `to_version` with no leftovers |
| `1` | Pin has reached `to_version` and leftover `old_callee` calls remain |
| `2` | Missing path / packet / pin, or invalid packet path |

Does not rewrite files. Use after Dependabot bumps the pin so CI fails until call sites are migrated (or until `conduit apply` / `conduit run` clears them).

```bash
conduit watch \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json
```

---

## `conduit apply`

Apply a packet only (no oracle tests, no verify).

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--packet` | required | Packet **file** path or **http(s) URL** |
| `--dry-run` | false | Print changes without writing |

---

## `conduit verify`

Write/update packet leftover-token oracle tests, then run the suite + self-correct using the packet for heuristic/LLM context. Same oracle + verify path as `conduit run`.

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--packet` | openai fixture if omitted | Packet **file** path or **http(s) URL** |
| `--max-retries` | `5` | |
| `--verbose` / `-v` | false | Self-correct failure/fix details |

---

## `conduit module`

### `module list`

List built-in / entry-point modules and whether they apply to installed manifests.

### `module new`

Scaffold a **profile-backed** detect module. On a TTY, prompts for source URLs (deprecations, changelog, OpenAPI, SDK repo, catalog). Flags skip prompts when provided.

| Option | Description |
|--------|-------------|
| `name` | Module name (argument) |
| `--package` | Package name (defaults to name) |
| `--ecosystem` | `pypi` (default) etc. |
| `--path` | Conduit package root or out dir |
| `--out-of-tree` | Standalone package layout |
| `--deprecations-url` | Vendor deprecations page |
| `--changelog-url` | Changelog / RSS |
| `--openapi-repo` | OpenAPI git URL |
| `--sdk-repo` | GitHub `org/name` for SDK releases |
| `--catalog-url` | Models/catalog docs URL |
| `--model-doc-template` | Per-id doc URL with `{model_id}` |
| `--live-catalog-url` | Live list API (e.g. `/v1/models`) |
| `--live-catalog-auth-env` | Env var for that API’s bearer token |
| `--evidence-hosts` | Comma-separated fetch allowlist |

---

## `conduit packet`

### `packet new`

Author a Migration Packet from source URLs (TTY prompts or flags). Writes `packets/{pkg}-{eco}-{version}.json` by default.

| Option | Description |
|--------|-------------|
| `--package` | Package name (prompted on TTY if omitted) |
| `--ecosystem` | `pypi` / `npm` / `go` / `maven` (default `pypi`) |
| `--version` / `--to` | Target version (single version; no from→to hop) |
| `--source-url` | Repeatable migrate guide / changelog / docs URL |
| `--out` | Output JSON path |
| `--no-enrich` | Skip LLM enrichment |
| `--scaffold-only` | Dependency hop + sources only (skip LLM) |

On a TTY, after package/ecosystem/version, prompts for source URLs until a blank line. Fetches URLs into `sources`; when an LLM is configured and sources are present, enriches rules by default. Without an LLM, still writes a valid packet with `DEPENDENCY_BUMP` + sources (never invents AST rules). Multi-statement gaps go in `side_effects`.

### `packet test`

Validate + summarize a packet. Optional dry-run apply (no verify / no credentials).

| Option | Description |
|--------|-------------|
| `--packet` | Packet JSON path (required) |
| `--path` | Optional consumer repo for dry-run apply + coverage |

Without `--path`, prints validity + summary (rules, sources, side_effects). Does not require `examples/*-consumer`.

### `packet init`

Scaffold an empty packet directory / file.

| Option | Description |
|--------|-------------|
| `--package` | required |
| `--from` / `--to` | versions |
| `--ecosystem` | `pypi` default |
| `--out` | output directory |

### `packet synthesize`

Build rules from `--changelog` / `--docs` (LLM if configured).

### `packet validate`

JSON Schema validation; exit non-zero on errors.

### `packet publish`

Validate a packet and place it in a **catalog** checkout under `by-package/<pkg>/<eco>/<packet_id>.json`. Catalog only; does **not** open consumer PRs.

| Option | Description |
|--------|-------------|
| `--packet` | Packet JSON path (required) |
| `--catalog` | Local catalog directory (git preferred) or git URL to shallow-clone |
| `--notice-url` | Optional webhook; POSTs `packet_id` / package / path metadata after write |
| `--no-commit` | Write files only; print `git add` / `commit` / `push` commands |

Second publish of the same hop is idempotent (same path, no duplicate commit when content matches). When `packet_id` lacks `-<ecosystem>-`, also writes root `<packet_id>.json` for slug fetch. Prefer a full raw URL while the catalog is private.

```bash
conduit packet publish \
  --packet ./examples/sample-packet/conduit-packet.json \
  --catalog /path/to/conduit-packets
```

### `packet show`

Pretty-print packet JSON.

### `packet from-detect`

Freeze **catalog snapshot** packets from a detect module. No consumer repo, apply, or source packet. Scan picks the latest stable SDK tag **per ecosystem** (PyPI and npm are different version lines).

```bash
conduit packet from-detect --module openai --out-dir ./packets
# --demo for offline fixtures
# --enrich optional LLM extra rules (scrape-only if omitted)
# --ecosystem pypi   # one chain only
```

Writes `{package}-{ecosystem}-{to_version}.json` (e.g. `openai-pypi-2.0.0.json`, `openai-npm-5.0.0.json`). First snapshot uses `from_version` `0`. Later runs write a **new** file whose `from` is the previous snapshot’s `to`. Same latest → skip, do not overwrite.

`--previous` and `--out` require `--ecosystem`.

Unknown `--module` or no scanned target version → exit 2.

### `packet new`

Guided hop packet: prefer `from-detect` when a detect module exists, else scaffold; validate; write; print a try-it line.

| Option | Description |
|--------|-------------|
| `--package` | Package name (prompted if omitted) |
| `--ecosystem` | `pypi` / `npm` / `go` / `maven` (default prompt: `pypi`) |
| `--from` / `--to` | Version hop |
| `--from-consumer` | Read `--from` from consumer pin under `--path` |
| `--path` | Consumer repo for `--from-consumer` / try-it path |
| `--enrich` | Optional LLM enrich (same as `from-detect --enrich`) |
| `--demo` | Offline detect fixtures |
| `--scaffold-only` | Skip from-detect; write empty scaffold |
| `--out` | Output JSON (default `packets/{pkg}-{eco}-{to}.json`) |

```bash
conduit packet new --package openai --ecosystem pypi --from 0.28.1 --to 1.0.0 --scaffold-only
conduit packet test --packet ./packets/openai-pypi-1.0.0.json --path ./examples/demo-consumer
```

### `packet diff-rules`

Summarize rules added/removed vs the previous hop snapshot (`--previous`, or search the packet’s directory).

```bash
conduit packet diff-rules ./packets/openai-pypi-1.40.0.json --previous ./packets/openai-pypi-1.0.0.json
```

### `packet test`

Validate + dry-run apply + coverage on `--path` (default `examples/demo-consumer`). No verify and no credential gate.

```bash
conduit packet test --packet ./packets/openai-pypi-1.0.0.json --path ./examples/demo-consumer
```

### `packet export-post-rules`

Append leftover-token / post-apply rules into a packet after a consumer run (see migration packets docs).

---

## Environment (global)

See [LLM configuration](llm.md) for `CONDUIT_LLM_*` and provider keys.

`GH_TOKEN` / `GITHUB_TOKEN` used by `gh` when opening PRs.
