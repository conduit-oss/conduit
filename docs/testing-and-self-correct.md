# Testing & self-correction

After apply, Conduit verifies the consumer repo still works.

## Test runner detection

[`test_runner.py`](../conduit/src/conduit/test_runner.py) looks for:

| Signal | Command |
|--------|---------|
| `pytest.ini`, `conftest.py`, `[tool.pytest`, or `tests/test_*.py` | `python -m pytest -q` |
| `package.json` scripts.test | `npm test --silent` |
| `go.mod` | `go test ./...` |

If nothing is detected, the runner currently treats the suite as a soft pass — unless test generation creates files first (see below).

## Packet-derived oracle tests

[`test_gen.ensure_tests`](../conduit/src/conduit/test_gen.py) runs at the start of **`conduit verify`** (and therefore also `conduit run`, which calls the same helper). It **always** regenerates a leftover-token oracle from the migration packet (even if the repo already has a suite):

- Python: `tests/test_conduit_oracle.py`
- npm: `conduit_oracle.test.js`

The file is self-contained (does not import Conduit). It scans the pruned/changed file set for **legacy tokens** from packet rules (`match`, `old_param`, `old_import`, `old_attr`, `old_callee`, and `DEPENDENCY_BUMP` pins). Matching uses the same whole-token boundaries as apply, so `gpt-4` does not flag `gpt-4-0613`.

Ignored paths (packet `ignore`, `.conduit/ignore.json`, auto-discovered `LEGACY_`/`FORBIDDEN_` contract files) are omitted from the scan. `REGEX_REPLACE` rules are skipped (no safe leftover string).

If the packet has **no** extractable tokens and the repo has **no** other tests, Conduit writes a one-line **import smoke** instead (no tautological `or True` asserts).

Optional LLM extra tests run only when no consumer tests exist besides the oracle, and they must not overwrite the oracle path.

Generated paths are included in the patch report / PR body when `conduit run` opens a PR. Oracle failures look like normal pytest/npm failures (`app.py still contains 'gpt-4-0613'`), so the self-correct loop can repair them.

`conduit apply` does **not** write the oracle (it only applies packet rules). `conduit run --skip-tests` skips oracle generation and verify.

Runners already cover pytest, `npm test`, and `go test ./...`. Java/Maven suites are not auto-detected yet — pass existing tests in-repo or generate via LLM.

## Self-correction loop

[`self_correct.verify_with_self_correct`](../conduit/src/conduit/self_correct.py):

1. Run tests  
2. On failure, up to `--max-retries` (default **5**):
   - Collect traceback file paths + nearby source/tests  
   - Build a **dynamic ignore list** (see below)  
   - If LLM configured → **Responses agent** (OpenAI: `gpt-5.4-mini`, `reasoning_effort=high`, tools such as `web_search` / `fetch_url` / local repo read-write / `grep` / `run_tests` / allowlisted `run_shell`). Seed URLs and suggested queries are provided; the model chooses tools. Final JSON may:
     - return `files` fixes (or write via `write_file`),
     - return `packet_patch` (rules/notes/sources) when the migration packet itself must change,
     - return `search_queries` on non-tool providers when evidence is still insufficient — Conduit runs those searches and asks again in the same attempt.
   - Else apply heuristic replaces derived from packet `EXACT_STRING_REPLACE` / `AST_PARAM_RENAME` (skipped on ignored files; contract-constant lines preserved)  
3. Re-run tests  
4. If still failing after retries → `conduit run` aborts PR creation (exit code 2)

Repair context seeds pytest short-trace paths (e.g. `openai_text/engines.py:25:`), packet/`import_files`, top-level package dirs, and `tests/` — not only `src/`.

If an LLM attempt produces **no** file edits, Conduit **nudges once** with explicit failing-path instructions (when retries remain) before early-stopping. Configure `CONDUIT_LLM_MAX_TURNS` (default 32) for longer tool loops. 429 rate limits are retried with backoff.

PR bodies include a **Rationale** section (each rule’s `reason`), **Notes** (decision log + self-correct), and full **Sources**.

### Dynamic ignore list

Heuristics and LLMs share [`repair_ignore.build_ignore_list`](../conduit/src/conduit/repair_ignore.py):

| Source | Example |
|--------|---------|
| Packet `ignore` | `"ignore": { "globs": ["**/policy.py"], "paths": [], "patterns": [] }` |
| Consumer `.conduit/ignore.json` | Same shape as packet `ignore` |
| Auto | Files that define `LEGACY_` / `FORBIDDEN_` / `EXPECTED_` / `ALLOWED_` / `MODERN_` constants whose values appear in packet match/old_param strings |

Ignored files are omitted from LLM context and heuristic scans. With `-v`, Conduit prints the ignore list at the start of self-correct.

With `--verbose` / `-v`, each attempt also prints:

- A truncated failure excerpt (stdout/stderr)
- Which context files were sent to the repair step
- Strategy used (`llm` or `heuristic`)
- Files updated and, for heuristics, each `old -> new` replacement
- When heuristics find nothing left to replace, why (already migrated / no matching rules)

If an attempt produces **no file edits** after the nudge (or with no LLM), Conduit stops early instead of repeating empty retries. Configure an LLM for deeper repairs, or fix remaining failures manually.

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

- Prefer real unit tests in the consumer repo; the generated oracle only proves leftover packet tokens are gone from scanned files.
- For local iteration without burning API quota, leave LLM unset and rely on packet quality + heuristics.
- CI should pass `CONDUIT_LLM_*` secrets only when you want the repair loop online.

## Related docs

- [LLM configuration](llm.md)
- [Pull requests](pull-requests.md)
- [Getting started](getting-started.md)
