# Testing & self-correction

After apply, Conduit verifies the consumer repo still works. **Skipped tests, dummy `except Exception` helpers, unused marker strings, and join-obfuscation never count as a pass.**

## Credentials

[`credentials.ensure_verify_credentials`](../conduit/src/conduit/credentials.py) runs at the start of **`conduit verify`** / **`conduit run`** (via `_verify_with_oracle`) **before** test generation:

- If the packet package is `openai`, or consumer tests/conftest mention `OPENAI_API_KEY`, Conduit requires `OPENAI_API_KEY` (or `OPENAI_KEY`).
- On a TTY it **prompts** (hidden input) and exports the value for this process and pytest subprocesses.
- Non-interactive (CI) **exits 2** if the key is missing. It does not continue verify.
- When an LLM will run (self-correct / functional test gen), it also requires `CONDUIT_LLM_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` unless the provider is `none`, `ollama`, or `custom`.

Set `CONDUIT_LLM_PROVIDER=none` to disable LLM while still supplying a consumer `OPENAI_API_KEY`.

## Test runner detection

[`test_runner.py`](../conduit/src/conduit/test_runner.py) looks for:

| Signal | Command |
|--------|---------|
| `pytest.ini`, `conftest.py`, `[tool.pytest`, or `tests/test_*.py` | `python -m pytest -q --tb=short` |
| `package.json` scripts.test | `npm test --silent` |
| `go.mod` | `go test ./...` |

`passed=True` only if the process exits 0 **and** pytest reports **at least one passed test**, with **zero failures/errors**. **All-skipped** (`N skipped, 0 passed`) is a failure (`all tests skipped`), including session autouse `pytest.skip` when `OPENAI_API_KEY` is unset. A missing suite is a failure, not a soft pass.

Stdout that says `OPENAI_API_KEY … is not set` is treated as missing credentials, not a green run.

## Packet-derived tests

[`test_gen.ensure_tests`](../conduit/src/conduit/test_gen.py) runs at the start of **`conduit verify`** (and therefore also `conduit run`). It regenerates:

| File | Role |
|------|------|
| `tests/test_conduit_oracle.py` (or `conduit_oracle.test.js`) | Leftover-token floor: old ids/paths/callees must be gone and not reconstructed via concat/join. `/v1/fine-tunes` also forbids `/fine-tunes`. |
| `tests/test_conduit_smoke.py` | New callees/params must appear as **real attribute/call use**, not unused `MIGRATION_MARKERS` tuples. |
| `tests/test_conduit_functional.py` | When an LLM is configured: live/functional tests of new endpoints and public APIs. **Must fail, not skip**, if the key is missing. |

Leftover scan includes import-pruned files **plus** neighbor modules, `configs/`, `scripts/`, `.github/`, compose/Docker files — not only files that `import openai`. Packet apply **and** heuristic self-correct **never rewrite** Conduit-generated `test_conduit_*` files (so the leftover `FORBIDDEN` list cannot be string-replaced into successor ids). LLM repair writes to those paths are rejected the same way.

Ignored paths (packet `ignore`, `.conduit/ignore.json`, auto-discovered contract files) are omitted from the leftover scan. Auto-ignore applies only to **tests/oracle/policy-style files** that **literally** assign `LEGACY_` / `FORBIDDEN_` to a quoted old token. Impl helpers named `LEGACY_ADA = "".join(["a","da"])` stay in the scan.

If the packet has **no** extractable tokens and the repo has **no** other tests, Conduit writes a one-line **import smoke** instead (no tautological `or True` asserts).

Existing consumer tests are **audited**, not trusted: skip/xfail/tautology notes are fed to functional test generation. They do not count as migration coverage.

`conduit apply` does **not** write these tests. `conduit run --skip-tests` skips generation and verify.

## Anti-cheat (separate from apply, inventory, and repair)

[`conduit.anticheat`](../conduit/src/conduit/anticheat/) fails verify when implementation cheats tests. **Mechanical rules always win.** An optional **LLM auditor** may only **add** findings; it cannot clear a mechanical fail.

Runs:

1. After packet apply (mechanical)
2. On every self-correct `write_file` (mechanical reject)
3. After pytest would pass (mechanical, then LLM auditor once)

Catalog is **packet/AST-derived**, not lab string needles: drop official `{package}` import; parallel HTTP to path tokens from packet rules without the SDK; synthetic `except`→literal responses; skip/xfail in generated tests; join **and f-string** leftover hides; writes to `packets/`, `.conduit/`, `vendor/`, knowledge `*.jsonl`; Path.read_text / sitecustomize / `_test_shim` oracle sanitizers; SDK monkeypatches (`{package}.<new_callee_root> =`, Compat wrappers); legacy kwargs on rewritten callees via `AST_PARAM_RENAME` (and optional `packet.anticheat.banned_kwargs_on` / `deny_substrings`).

**Migration audit log.** Apply, test-gen, repair writes/rejects/restores, and mechanical snapshots append to `.conduit/migration_audit.jsonl` (meta in `migration_audit_meta.json`). The **LLM auditor is still an agent** (max 4 turns) but is **log-first**: it is seeded with the audit log and may only `read_file` / `grep` paths listed there (no `list_files`). It returns additive `cheats` plus an advisory `score` (`honesty`, `migration_completeness`); the score is **non-gating** and appears in the run summary. Mechanical findings always win.

Keep the official SDK at `to_version` and migrate call sites. Generated REST clients and first-party SDK subclasses from the same package are allowed. Scans skip `.conduit/` (including downloaded exports).

`CONDUIT_ANTICHEAT_STRICT=1` fails closed if the LLM auditor crashes; default logs a warning and keeps the mechanical result.

## Integrity audit (does not trust pytest)

The integrity helpers in [`integrity.py`](../conduit/src/conduit/integrity.py) still detect dummy-except and marker tuples; repo scans go through anti-cheat.

Self-correct may only fix implementation by using the real new API — not by adding fallbacks or renaming things `LEGACY_*`.

## Self-correction loop

[`self_correct.verify_with_self_correct`](../conduit/src/conduit/self_correct.py):

1. Run tests + integrity audit  
2. On failure, up to `--max-retries` (default **5**):
   - Build **Cursor-shaped repair context**:
     - structured failure (`failed_nodes`, leftover tokens, fingerprint)
     - **±60-line windows** from traceback paths (≤8 impl windows + primary failing test + `conftest`), not up to 24 full modules
     - `import_files` only when they appear in the traceback (no flood)
     - **`repair_journal`** from the migration audit log (prior writes / rejects / restores)
   - Build a **dynamic ignore list** (see below)  
   - If LLM configured → **Responses agent** with **scoped tools** (`read_file` / `grep` / `write_file` / focused `run_tests(nodeids=…)` / allowlisted `run_shell` / `web_search` / `fetch_url`). **`list_files` is omitted** so the agent cannot inventory the whole repo; off-allowlist reads are rejected. Writes that obfuscate leftover tokens (`"".join(...)`), swallow exceptions, add marker tuples, edit leftover/smoke/functional tests, weaken tests (`skip`/`xfail`), or touch `vendor/` are **rejected**. Instructions are **edit-first**: act on seeded windows, smallest write, then focused retest. Final JSON may:
     - return `files` fixes (or write via `write_file`),
     - return `packet_patch` (rules/notes/sources) when the migration packet itself must change,
     - return `search_queries` on non-tool providers when evidence is still insufficient — Conduit runs those searches and asks again in the same attempt.
   - Else apply heuristic replaces derived from packet `EXACT_STRING_REPLACE` / `AST_PARAM_RENAME` (skipped on ignored files; contract-constant lines preserved)  
3. Re-run **full** tests + integrity (mid-loop `run_tests` may use focused nodeids only)  
4. If still failing after retries → `conduit run` aborts PR creation (exit code 2)

Repair context prefers failing spans + journal over dumping every packet import. Same-dir siblings of traceback hits are allowlisted for `read_file`/`grep` even when not seeded as windows.

If an LLM attempt produces **no** file edits, Conduit **nudges once** with explicit failing-path instructions (when retries remain) before early-stopping. Configure `CONDUIT_LLM_MAX_TURNS` (default 32) for longer tool loops. 429 rate limits are retried with backoff.

PR bodies include a **Rationale** section (each rule’s `reason`), **Notes** (decision log + self-correct), and full **Sources**.

### Dynamic ignore list

Heuristics and LLMs share [`repair_ignore.build_ignore_list`](../conduit/src/conduit/repair_ignore.py):

| Source | Example |
|--------|---------|
| Packet `ignore` | `"ignore": { "globs": ["**/policy.py"], "paths": [], "patterns": [] }` |
| Consumer `.conduit/ignore.json` | Same shape as packet `ignore` |
| Auto | Test/oracle/policy files that **literally** assign `LEGACY_` / `FORBIDDEN_` / `EXPECTED_` / `ALLOWED_` / `MODERN_` to a quoted packet match string |

Impl files are never auto-ignored just because they define a `LEGACY_*` name. Ignored files are omitted from LLM context and heuristic scans. With `-v`, Conduit prints the ignore list at the start of self-correct.

With `--verbose` / `-v`, each attempt also prints:

- A truncated failure excerpt (stdout/stderr)
- Which context **windows** were seeded for repair (and allowlist size)
- Strategy used (`llm` or `heuristic`)
- Files updated and, for heuristics, each `old -> new` replacement
- When heuristics find nothing left to replace, why (already migrated / no matching rules)

If an attempt produces **no file edits** after the nudge (or with no LLM), Conduit stops early instead of repeating empty retries. Configure an LLM for deeper repairs, or fix remaining failures manually.

Conduit also stops early (remaining `--max-retries` unused) when a failure is treated as **unpassable**:

- **Anticheat** failure (initial or after an attempt) — integrity, not a migratable pytest fail
- **All repair writes rejected** (and no successful `packet_patch`)
- **No progress** — same failure fingerprint for 2 consecutive attempts (e.g. identical leftover-oracle nodes)
- **Repair regressed twice** — two consecutive snapshot restores after collection/import breakage

Early-stop reasons are printed as `[self-correct] stopping early: …` and recorded on `TestResult.fail_reason`.

```bash
conduit run -v --path . --packet openai --skip-pr
```

## Commands

```bash
conduit verify --path . --packet ./conduit-packet.json --max-retries 5
conduit run ... --max-retries 3
conduit run -v ... --max-retries 3   # show failure + fix details per attempt
conduit run ... --skip-tests    # apply only; skips oracle + verify
```

## Tips

- Leftover oracles are a **floor**. Functional tests + **anti-cheat** are the **ceiling**.
- Cheating (join/f-string obfuscation, fake HTTP clients, skip-all conftest, dropping the official SDK) fails verify even if pytest exits 0.
- For local iteration without LLM quota, set `CONDUIT_LLM_PROVIDER=none` and still export `OPENAI_API_KEY` for consumer tests.
- CI should pass `OPENAI_API_KEY` (consumer) and `CONDUIT_LLM_*` only when the repair loop should run.

## Related docs

- [LLM configuration](llm.md)
- [Pull requests](pull-requests.md)
- [Getting started](getting-started.md)
