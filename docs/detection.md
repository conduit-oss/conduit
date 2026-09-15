# Detection

Detection answers: **what changed in this repo’s dependencies / usage, and (optionally) what vendor signals exist?**

Command: `conduit detect` (also used inside `conduit run` when synthesizing a packet).

The **OSS hero path** is packet author → test → apply. Detect’s default story is **repo reality**: lockfile/manifest diffs + client usage scan + coverage against a packet when one is present. Vendor modules (`--module openai`) are **optional / reference** — not required to author or apply a Migration Packet.

## Sources

### 0. Client package state (pre-step)

Before vendor modules run, Conduit builds a **`PackageClientState`** per applicable package (installed version, model ids / API tokens found in the repo, import files, ecosystems). Regex always; optional LLM enrichment when configured. Empty model lists mean **unknown**, not safe.

When an LLM is configured, enrichment still runs as an **agent**, but it is seeded with a mechanical **usage dossier** (path allowlist + regex/import hit index from the cheap scan). The agent may only `read_file` / `grep` allowlisted paths (no repo-wide `list_files`), with a lower turn cap. Merged tokens must still appear verbatim in the consumer corpus.

If the dossier has **no `gaps_to_check`** and every hit token is already in `already_found`, enrichment is **skipped** (no agent turns). Otherwise the agent investigates only those gaps; small gap lists use a tighter turn budget (≤3).

Vendor modules consume this as the client baseline. See [Detect modules](detect-modules.md).

### 1. Lockfile / manifest git diff

Conduit diffs watched files against `--base-ref` (or `HEAD~1` / merge-base heuristics when omitted):

- `requirements.txt`, `requirements-dev.txt`
- `pyproject.toml`
- `package.json`, `package-lock.json`
- `poetry.lock`, `Pipfile.lock`
- `go.mod`

Only packages whose declared version **changed** become bump `VersionJump` signals. Names that appear only on the new side become `PACKAGE_ADDED`; names only on the old side become `PACKAGE_REMOVED`. Unchanged dependencies are ignored (Tier 1 isolation). Add/remove events are not filtered by `--majors-only`.

Companion ADD/REMOVE rules are folded into the primary packet only when that packet already names the package (do not invent companions from the same PR). Leftover lockfile events still appear under Double-check.

Separately, Conduit always parses **currently declared** versions from the same manifests via `read_installed()` (`DetectResult.installed`). Pip and npm pins are stored separately; the flat map keeps the **PyPI** pin when both exist. Catalog bind uses the pin for the packet’s ecosystem. That map is used to:

- Decide whether a detect module **applies** to this repo
- Fill packet **`from_version`** when there is no lockfile jump signal (so export delta can fetch the real old package)

By default only **major** bumps are treated as migration candidates (`--majors-only`, on by default). Use `--all-bumps` to include minor/patch.

Vendor SDK release workers prefer the **next intermediate version step** (one major at a time under `--majors-only`), not a jump straight to GitHub latest. Newer majors are recorded as deferred. Shared helper: [`version_steps.py`](../conduit/src/conduit/detect/version_steps.py).

Ecosystem parsers today:

| File | Quality |
|------|---------|
| `requirements.txt` | Solid (`pkg==ver` / operators) |
| `package.json` | Solid (deps / dev / peer) |
| `go.mod` | Supported |
| `pyproject.toml` | Best-effort regex (not full TOML) |
| `package-lock.json` / `poetry.lock` | Watched; parsing is weaker — prefer comparing manifest side when possible |

Implementation: [`conduit/src/conduit/detect/lockfile_diff.py`](../conduit/src/conduit/detect/lockfile_diff.py).

### 2. Vendor detect modules

Modules implement `DetectModule` and emit `ChangeSignal`s (renames, deprecations, suggested rules). Built-in: **`openai`**.

```bash
conduit detect --path . --module openai
conduit detect --path . --module openai --demo            # offline fixtures
conduit detect --path . --skip-lockfile --module openai   # modules only
conduit detect --path . --skip-modules                    # lockfile only
conduit detect --path . --json                           # machine-readable
```

See [Detect modules](detect-modules.md).

## Signals → package selection

`conduit run` picks a package via:

1. `--package` if set
2. Else package name from `--packet <name>` when `--packet` is not a file path
3. Else prefer `openai` if present in signals
4. Else first package among signals

Those signals (plus `installed` versions) seed packet synthesis when no cached/explicit packet file exists. See [Migration packets](migration-packets.md).

## Dependabot / Renovate intercept

In CI, pass the PR base so the lockfile diff sees the bump:

```bash
conduit run --path . --base-ref origin/main --skip-pr
```

Workflow: [GitHub Actions](github-actions.md).

## Related docs

- [Architecture](architecture.md)
- [Detect modules](detect-modules.md)
- [CLI reference](cli-reference.md)
