# Detect modules

Detect modules are **vendor-specific plugins** that emit `ChangeSignal`s (API renames, deprecations, suggested packet rules) beyond raw lockfile diffs.

**Public positioning:** the OSS hero path is [`packet new` → `packet test` → apply](migration-packets.md). Modules are **advanced / reference** tooling. Keep **OpenAI** as the supported reference module; do not market `module new` scaffolds as fully supported vendors.

## Interface

```python
class DetectModule(ABC):
    name: str = "module"
    packages: list[str] = []          # e.g. ["openai"]

    def applies(self, installed: dict[str, str]) -> bool:
        ...

    def run(self, ctx: DetectContext) -> list[ChangeSignal]:
        ...
```

`DetectContext` includes `repo_root`, `installed` versions, **`package_states`** (client baseline from the pre-step), and `demo` (offline fixtures vs live sources).

## Client state pre-step

Before vendor modules run, Conduit scans the client repo per applicable package:

1. **Regex (always)** — import-pruned source files + common config suffixes; vendor pattern packs extract model ids / API shape tokens. Manifests supply per-ecosystem pins (`pypi` vs `npm`); catalog bind uses the packet’s ecosystem.
2. **LLM (optional)** — when an LLM is configured and not `--demo`, a small structured pass may merge additional tokens **only if they appear verbatim in provided snippets**. Failures are soft warnings; regex state is kept.

Empty `model_ids` means **unknown**, not all-clear (env/dynamic construction may hide usage).

This baseline is the **source packet** (in-memory `package_states`, also written to `.conduit/source-packets/<package>.json`). After the migration packet is built, `conduit run` / `conduit detect` print:

1. Source packet (models / api_patterns / files)
2. Migration packet summary (signals + rules)
3. **Coverage** — each client model/api_pattern as `WILL MIGRATE`, `KEEP`, `NO RULE`, or `UNMAPPED`

`NO RULE` is “used here, packet has no migrate-from rule” (not necessarily a bug). `KEEP` is a successor/current id. `UNMAPPED` wrappers are not gaps. `-v` adds full JSON dumps.

See [`conduit/src/conduit/detect/client_state.py`](../conduit/src/conduit/detect/client_state.py) and [`coverage.py`](../conduit/src/conduit/detect/coverage.py).

Built-ins register via entry point group:

```toml
[project.entry-points."conduit.detect_modules"]
openai = "conduit.detect.modules.openai:OpenAIModule"
```

Discovery: [`conduit/src/conduit/detect/modules/discovery.py`](../conduit/src/conduit/detect/modules/discovery.py).

## Built-in: OpenAI

`OpenAIModule` runs several workers. **Live by default** (network). Pass `--demo` for offline fixtures (CI / sample packet demos).

| Worker | Live source | Client baseline | Demo fixture |
|--------|-------------|-----------------|--------------|
| OpenAPI diff | Fixture `previous.yaml` (baseline) vs freshly cloned `openai/openai-openapi` latest | — | `fixtures/openai/openapi/` (previous vs latest) |
| Deprecation scraper | `https://platform.openai.com/docs/deprecations` | — | `fixtures/openai/deprecations/` |
| Model polling | `GET /v1/models` (needs `OPENAI_API_KEY`) | `package_states["openai"].model_ids` | `fixtures/openai/models/` (+ `client_used.json` when no scan) |
| Changelog parser | platform changelog page | — | `fixtures/openai/changelogs/` |
| SDK release | GitHub releases list (stepped) | installed openai version + ecosystems | `fixtures/openai/sdk_releases/` |

SDK release bumps **one major at a time** (or next published release with `--all-bumps`) via [`version_steps.next_version_step`](../conduit/src/conduit/detect/version_steps.py). If newer majors exist, they are noted as deferred in the signal reason.

After workers run, **endpoint compat** checks each model A→B against the replacement’s Supported endpoints on [developers.openai.com/api/docs/models](https://developers.openai.com/api/docs/models) (`.md` pages; demo uses `fixtures/openai/model_docs/`). Client `api_patterns` map to required routes (e.g. `chat.completions` → `v1/chat/completions`). Incompatible replacements are swapped for a catalog alternate that supports those routes, or cleared with a note — never invented. Each decision carries a `reason` onto rules / packet notes for the PR.

**Path-param compat** then joins endpoint A→B pairs (deprecation `/v1/old`→`/v1/new`, or OpenAPI removal + documented successor) with OpenAPI request-body schemas (`previous[old]` vs `latest[new]`). A 1:1 property rename becomes `AST_PARAM_RENAME` with grounded `function_target`(s) from client `api_patterns` and the OpenAI path→SDK callee table (e.g. `/v1/chat/completions` → `chat.completions.create`). Shared-path OpenAPI renames (same path in both specs) get the same treatment. Removed props without a 1:1 partner are noted only — never invented.

Normalize emits apply rules **only when grounded**: model A→B when scrape states both; path replace only when both `/v1/...` sides are known; `AST_PARAM_RENAME` only when the signal carries explicit `function_target`(s).

Call-shape / successor gaps are filled by **evidence + LLM packet enrichment** (module seed URLs + web search) when an LLM is configured — see [LLM configuration](llm.md).

Clean “0 signals” from OpenAPI/changelog/SDK/model-polling workers are **verbose-only** (`-v`) when the live source ran successfully. Model polling **always warns** when `OPENAI_API_KEY` is missing or `GET /v1/models` fails (and the client has model ids to check). Missing client model ids or installed SDK version are verbose-only (unknown baseline).

Vendor APIs/docs are the **change** source; the client-state pre-step is the **baseline**. Conduit does not rely on local `.models-snapshot.json` / tag snapshots for emit decisions.

```bash
conduit detect --path . --module openai          # live
conduit detect --path . --module openai --demo   # fixtures
conduit run --path . --packet openai -v          # live detect
conduit run --path ./examples/demo-consumer --packet ./examples/sample-packet/conduit-packet.json --demo --skip-pr
```

Normalize step turns raw events into `ChangeSignal` + `suggested_rules` that packet synthesis can fold in.

## Scaffold a new module

New vendors share OpenAI’s pipeline via a **VendorProfile** (docs URLs, OpenAPI repo, SDK releases, catalog, evidence hosts). `conduit module new` asks for those links on a TTY (or accept `--deprecations-url` / `--openapi-repo` / … flags) and emits `profile.py` plus a `run()` that calls `run_profile_module`.

OpenAI is the reference profile: [`conduit/src/conduit/detect/modules/openai/profile.py`](../conduit/src/conduit/detect/modules/openai/profile.py).

```bash
conduit module list
conduit module new stripe --package stripe --ecosystem pypi --path ./conduit
```

This creates `src/conduit/detect/modules/<name>/` with `profile.py`, fixtures dir, and a smoke test. HTML deprecation/changelog scrapers stay OpenAI-specific (`parser_kind=generic`); add a custom worker when the vendor’s pages don’t match. Wire the module into entry points / `_builtin_modules` so it auto-loads.

Out-of-tree modules:

```bash
conduit module new acme --package acme --out-of-tree --path ./my-acme-module
```

Then install that package so its `conduit.detect_modules` entry point is visible.

## Filtering at runtime

```bash
conduit detect --module openai
conduit run --module openai
conduit run --skip-modules          # lockfile only
```

## Related docs

- [Detection](detection.md)
- [Migration packets](migration-packets.md)
- [CLI reference](cli-reference.md)
