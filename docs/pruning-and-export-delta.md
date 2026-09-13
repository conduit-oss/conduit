# Pruning & export delta

After detection, Conduit shrinks the file set before building ASTs or applying rules.

## Import prune (Tier 2)

`prune_by_imports(root, packages)` scans source files (`.py`, `.ts`, `.js`, `.tsx`, `.jsx`, `.java`, `.go`) for import-like strings:

- `from openai import …` / `import openai`
- `from "openai"` / `import("openai")` / `require("openai")`
- Java `import com.openai…;` / Go `"module/path"` import paths

Hard exclusions (never scanned):

`node_modules/`, `vendor/`, `dist/`, `build/`, `.git/`, `.venv/`, `venv/`, `__pycache__/`, `.conduit/`, and similar.

Implementation: [`conduit/src/conduit/prune/grep_imports.py`](../conduit/src/conduit/prune/grep_imports.py).

If **zero** files match, apply falls back to a broader candidate set (still excluding hard dirs) so dependency bumps can still land.

## Export delta (Tier 3)

Export delta compares the **public API surface** of the dependency at the packet’s `from_version` vs `to_version`, then prefers consumer files that mention changed symbols.

When both versions are resolvable, Conduit:

1. Downloads both package versions into `.conduit/exports/` (via `pip download` or `npm pack`)
2. Extracts public symbols
   - **Python:** `__all__`, public classes/functions/assigns, shallow submodule names
   - **npm:** `.d.ts` / `package.json` `types` / `exports` keys
3. Diffs → `added`, `removed`, `renamed` (heuristic renames for case / token overlap)
4. Further prefers files that mention changed symbols

### Version inputs

Export delta reads the **packet top-level** versions (not only lockfile jumps):

- `from_version` — the version the project is on now. Catalog packets with floor `0` are stamped from the **packet-ecosystem** client pin before export-delta runs.
- `to_version` — migration target (from detect signals or `DEPENDENCY_BUMP` rules)

If either side cannot be fetched (missing placeholder like `0.0.0`, network/pip failure, etc.), Conduit **soft-fails** and continues with the import-pruned set:

```text
Export delta skipped: could not resolve openai==0.0.0 (pypi)
```

With `--verbose` / `-v`, Conduit also prints per-side diagnostics (e.g. placeholder vs `pip download` stderr).

Disable with:

```bash
conduit run ... --skip-export-delta
```

Implementation: [`conduit/src/conduit/export_delta/`](../conduit/src/conduit/export_delta/).

### Cache layout

```text
.conduit/exports/
  pypi/<package>/<version>/
  npm/<package>/<version>/
```

Safe to delete; Conduit will re-fetch.

## Vendor context gate

Even after pruning, each rule application can require “vendor context” in the file (e.g. mentions of `openai`) so unrelated hits are less likely. See `context_filter.py`.

## Related docs

- [Migration packets](migration-packets.md) (how `from_version` / `to_version` are chosen)
- [Architecture](architecture.md)
- [Codemods](codemods.md)
