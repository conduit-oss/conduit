# CLI reference

Entry point: `conduit` (Typer app in [`conduit/src/conduit/main.py`](../conduit/src/conduit/main.py)).

```bash
conduit --help
conduit <command> --help
```

Top-level `--help` keeps map, watch, impact, service, packet, verify, and run in **Commands**. `detect` and `module` appear under **Advanced**. `conduit apply` is deferred and is not a command.

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

Two modes.

**Outside-service watch** (no `--packet`). Read the map and keep outside services only: external API clients and REST, database drivers, cloud SDKs, and incoming webhooks. A newer published version is the trigger. The change list is a diff of the old and new PyPI wheels (removed, renamed, and changed-signature functions, and return types), the botocore service model, and the webhook schema. GitHub releases and changelogs are extra hints. There is no hand-written change list. `impacts` lists each broken call and payload field with `file:line` and a confidence. A version bump with no call-site hit is not reported. This is not a Dependabot notification and it does not watch internal repos. `--feeds` points at recorded copies of those public URLs so tests and demos do not use the network. Omit it to fetch the URLs. Recorded wheels and schemas live next to that directory in `conduit/fixtures/impact`.

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--feeds` | live HTTP | Recorded public-feed copies (`index.json` maps each URL to a file). Not a change list |
| `--out` | none | Write one JSON packet per file |
| `--service` | every service on the map | One catalogued service |
| `--json` | false | `{services, skipped, warnings, packets, impacts}` |
| `--show-callers` | false | List wrapper callers. The default puts them in a low-confidence section |

Exit `0` after a feed pass, including when nothing changed. Exit `2` when `--path` or `--feeds` is not a directory.

```bash
conduit watch \
  --path ./examples/wrapped-openai-fixture \
  --feeds ./conduit/fixtures/feeds \
  --json
```

**Leftover gate** (`--packet`). Read-only. Scans for packet `old_callee` leftovers and compares the consumer pin to the packet hop. This is the apply check, not a watcher for another repo.

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--packet` | none | Packet **file** path or **http(s) URL**. Presence selects this mode |
| `--json` | false | Machine-readable verdict (`status`, `exit_code`, leftovers) |

Exit codes for the leftover gate:

| Exit | When |
|------|------|
| `0` | Pin still at `from_version` (prints a **warn** if leftovers exist), or pin at `to_version` with no leftovers |
| `1` | Pin has reached `to_version` and leftover `old_callee` calls remain |
| `2` | Missing path / packet / pin, or invalid packet path |

Does not rewrite files. This is the leftover check for a hop you already have. It is not a Dependabot intercept, a lockfile bump, or a watch of another repo. CI can fail while old call sites remain after the pin reaches `to_version`. Clearing them with `conduit apply` is deferred; `conduit run` still rewrites a tree from a frozen packet.

Wrapped call sites are included for catalogued services (openai, anthropic, stripe, twilio, boto3). A function that calls `llm.complete` is a leftover while `complete` still reaches a packet `old_callee`. See [Outside-in mode](outside-in.md).

```bash
conduit watch \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json
```

---

## `conduit map`

Read-only outside-in call graph. Inventory external API clients and REST hosts, database drivers, cloud calls (service + operation, such as S3 through botocore), and incoming webhook handlers. Follow wrappers, aliases, and re-exports. Python only. With `--packet`, report the blast radius of that hop.

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--service` | catalog | One service id or Python module (`openai`, `stripe`, …) |
| `--packet` | none | Packet file or http(s) URL. Adds blast radius for `package` |
| `--json` | false | Machine-readable map (`entries`, `chains`, `blast`) |
| `--markdown` | false | Markdown report |

The graph follows wrappers, aliases, re-exports, `functools.partial`, `getattr` on a known client with a string literal, and a lambda assigned to a name. Imports inside a function body are tracked, including an import in an enclosing function. `requests`, `httpx`, and `aiohttp` calls are mapped by host and path when the URL, an f-string, a constant, or a client `base_url` can be resolved. A variable passed to `getattr` stays unresolved.

Exit `2` when `--path` is not a directory or `--packet` is missing or invalid. Exit `1` when the packet fails schema validation. Exit `0` after a scan, including an empty map.

## `conduit impact`

On-demand line-by-line impact for one outside service: `conduit impact --dep X [--from V] [--to V]`. Compares the pin (or `--from`) with `--to`, or with the newer surface in `--fixtures`. Diffs public API wheels for removed, renamed, and changed-signature functions and return types, a botocore service model, and a webhook payload schema. GitHub releases and changelogs are hints only. Prints `file:line` for call sites in this repo. Does not edit the lockfile, scan CVEs, or report ordinary libraries.

| Option | Default | Description |
|--------|---------|-------------|
| `--path` | `.` | Repo root |
| `--dep` | required | Service or client (`sqlalchemy`, `boto3`, `stripe`, `acme-sdk`) |
| `--to` | recorded newer surface | Version to compare against the pin |
| `--from` | the pin in this repo | Version to compare from when the repo does not pin one, or the pin does not match a recorded surface |
| `--fixtures` | none (fetch small wheels from PyPI) | Offline wheels, models, and schemas |
| `--json` | false | `{dependency, impacts, notes}` |
| `--show-callers` | false | List wrapper callers. The default puts them in a low-confidence section |

Exit `2` when `--dep` is an ordinary library, or `--path` / `--fixtures` is missing. Exit `0` when the check finishes. The version compared from is `--from`, else an exact pin, else a lockfile (`poetry.lock`, `uv.lock`, `Pipfile.lock`), else the installed distribution, else the highest PyPI release matching a requirement range. `--to` or the latest release is the other side. If no version can be determined, the text is `not compared` and names the reason (no pin, lock, install, or resolvable range, or network disabled for a range) rather than an all-clear. A pin that does not match a recorded surface prints `not compared: pinned version <version> does not match a recorded surface`. An all-clear (`No call sites in this repo are hit by the released change.`) means the surfaces were compared and no call site was hit.

The primary finding is the line a concrete change affects: a call, a callable that is loaded but not called (`return pkg.Widget.create`), a write of a module global the new wheel no longer assigns (`pkg.api_base = ...`), or a dict read of a return the wheel classifies as dict-like before and typed after. Callers of a wrapper are listed under `low confidence` and are not part of that count; `--show-callers` expands them. `getattr(package, "__version__")` is the same version read as `package.__version__`. A name the new wheel still exports is not reported as removed. REST calls are compared with a versioned OpenAPI pair when one is recorded. Without that pair the note names the calls that had no spec. A single latest document from a public spec repo is not treated as a diff. Changelog lines that say an endpoint was removed or renamed are low-confidence hints.

```bash
conduit impact \
  --dep boto3 \
  --path ./examples/service-clients \
  --fixtures ./conduit/fixtures/impact
```

## `conduit service`

Hosted service skeleton. No GitHub App. Each customer gets a stored map. `tick` (one pass) or `run` (loop) watches the outside services on that map and diffs the same wheels, botocore models, and webhook schemas as `conduit watch`. A hit flags affected call sites and writes a markdown note under the store. Rewriting the tree is deferred: `conduit apply` is not a command, and `--apply` does not write files. Python only.

| Command | What it does |
|---------|----------------|
| `service add --customer ID --path REPO --store DIR` | Scan the repo and store the map |
| `service tick --store DIR [--feeds DIR] [--customer ID] [--apply]` | One scheduled pass |
| `service run --store DIR [--interval 3600] [--once] [--feeds DIR] [--apply]` | Repeat `tick` |

```bash
conduit service add --customer acme --path ./examples/wrapped-openai-fixture --store ./.conduit-service
conduit service tick --store ./.conduit-service --feeds ./conduit/fixtures/feeds
```

```bash
conduit map \
  --path ./examples/wrapped-openai-fixture \
  --service openai \
  --packet ./examples/sample-packet/conduit-packet.json
```

Python only. A variable passed to `getattr`, dependency injection, and positional argument forwarding are not resolved. A string literal passed to `getattr`, `functools.partial`, and a lambda that calls the SDK are. Details: [Outside-in mode](outside-in.md).

---

## `conduit apply` (deferred)

`conduit apply` is not a command. Rewriting a tree from the CLI is deferred. `conduit run` still calls the same packet engines (no LLM on the rewrite itself). Tests that need the rewrite call `apply_migration` in process.

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

Author a Migration Packet from source URLs (TTY prompts or flags). Writes `packets/{pkg}-{eco}-{version}.json` by default. Top-level `from_version` is `*` at author time.

| Option | Description |
|--------|-------------|
| `--package` | Package name (prompted on TTY if omitted) |
| `--ecosystem` | `pypi` / `npm` / `go` / `maven` (default `pypi`) |
| `--version` / `--to` | Target version (single version; no from→to hop) |
| `--source-url` | Repeatable migrate guide / changelog / docs URL |
| `--out` | Output JSON path |
| `--no-enrich` | Skip LLM enrichment |
| `--scaffold-only` | Dependency hop + sources only (skip LLM) |

On a TTY, after package/ecosystem/version, prompts for source URLs until a blank line. Fetches URLs into `sources`. When an LLM is configured and sources are present, enriches rules by default. Without an LLM, still writes a valid packet with `DEPENDENCY_BUMP` + sources (never invents AST rules). Multi-statement gaps go in `side_effects`.

```bash
conduit packet new \
  --package google-genai --ecosystem pypi --version 1.0.0 \
  --source-url https://googleapis.github.io/python-genai/
```

### `packet test`

Validate + summarize a packet. Optional dry-run apply (no verify / no credentials).

| Option | Description |
|--------|-------------|
| `--packet` | Packet JSON path (required) |
| `--path` | Optional consumer repo for dry-run apply + coverage |

Without `--path`, prints validity + summary (rules, sources, side_effects). Does not require `examples/*-consumer`.

```bash
conduit packet test --packet ./packets/google-genai-pypi-1.0.0.json
conduit packet test --packet ./packets/google-genai-pypi-1.0.0.json --path ./examples/demo-consumer
```

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

### `packet init`

Scaffold an empty packet directory / file. Explicit `--from` / `--to` hop (not used by `packet new`).

| Option | Description |
|--------|-------------|
| `--package` | required |
| `--from` / `--to` | versions |
| `--ecosystem` | `pypi` default |
| `--out` | output directory |

### `packet synthesize`

Build rules from `--changelog` / `--docs` (LLM if configured). Requires `--package`, `--from`, `--to`.

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

### `packet diff-rules`

Summarize rules added/removed vs the previous hop snapshot (`--previous`, or search the packet’s directory).

```bash
conduit packet diff-rules ./packets/openai-pypi-1.40.0.json --previous ./packets/openai-pypi-1.0.0.json
```

### `packet export-post-rules`

Append leftover-token / post-apply rules into a packet after a consumer run (see migration packets docs).

---

## Environment (global)

See [LLM configuration](llm.md) for `CONDUIT_LLM_*` and provider keys.

`GH_TOKEN` / `GITHUB_TOKEN` used by `gh` when opening PRs.
