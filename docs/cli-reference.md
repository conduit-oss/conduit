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
| `--packet` | cache/synth | Path to a packet JSON, **http(s) URL**, or a package name (e.g. `openai`, `stripe`). A file or URL **loads** the packet (no OpenAI scrape / synthesis). A package name still synthesizes from detect. |
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
3. Otherwise treat it as a **package name** → same idea as `--package <name>`: detect/synthesize a packet for that package.
4. If both `--packet <file-or-url>` and `--package` disagree on the package field, the **packet file** wins (with a warning).
5. If `--packet <name>` and `--package` disagree, **`--package`** wins (with a warning).

A **file or URL** skips vendor detect workers (no GitHub/OpenAI scrape). Conduit still scans the consumer repo for a **source packet** (imports / models) so prune and coverage work. `--packet openai` (a name) still runs vendor detect and synthesizes.

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

---

## Environment (global)

See [LLM configuration](llm.md) for `CONDUIT_LLM_*` and provider keys.

`GH_TOKEN` / `GITHUB_TOKEN` used by `gh` when opening PRs.
