# Testing & self-correction

After apply, Conduit verifies the consumer repo still works.

## Test runner detection

[`test_runner.py`](../conduit/src/conduit/test_runner.py) looks for:

| Signal | Command |
|--------|---------|
| `pytest.ini`, `conftest.py`, `[tool.pytest`, or `tests/test_*.py` | `pytest -q` (or `python -m pytest -q`) |
| `package.json` scripts.test | `npm test --silent` |
| `go.mod` | `go test ./...` |

If nothing is detected, the runner currently treats the suite as a soft pass — unless test generation creates files first (see below).

## Generating tests when missing

[`test_gen.ensure_tests`](../conduit/src/conduit/test_gen.py) runs before verify when you use `conduit run` (unless `--skip-tests`):

1. If a runner **and** test files already exist → no-op  
2. Else if an LLM is configured → ask it for a minimal smoke test JSON (`files` map)  
3. Else write a **deterministic stub**:
   - Python: `tests/test_conduit_migration.py` (import package + marker assert)
   - npm ecosystem: `conduit_migration.test.js`

Runners already cover pytest, `npm test`, and `go test ./...`. Java/Maven suites are not auto-detected yet — pass existing tests in-repo or generate via LLM.

Generated paths are included in the patch report / PR body.

## Self-correction loop

[`self_correct.verify_with_self_correct`](../conduit/src/conduit/self_correct.py):

1. Run tests  
2. On failure, up to `--max-retries` (default **5**):
   - Collect traceback file paths + nearby source/tests  
   - Build a **dynamic ignore list** (see below)  
   - If LLM configured → **general web research** (vendor docs seeds + open web search from the failure text/packet). The model may:
     - return `files` fixes,
     - return `packet_patch` (rules/notes/sources) when the migration packet itself must change,
     - return `search_queries` when evidence is insufficient — Conduit runs those searches and asks again in the same attempt.
   - Else apply heuristic replaces derived from packet `EXACT_STRING_REPLACE` / `AST_PARAM_RENAME` (skipped on ignored files; contract-constant lines preserved)  
3. Re-run tests  
4. If still failing after retries → `conduit run` aborts PR creation (exit code 2)

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

If an attempt produces **no file edits**, Conduit stops early instead of repeating empty retries. Configure an LLM for deeper repairs, or fix remaining failures manually.

```bash
conduit run -v --path . --packet openai --skip-pr
```

## Commands

```bash
conduit verify --path . --packet ./conduit-packet.json --max-retries 5
conduit run ... --max-retries 3
conduit run -v ... --max-retries 3   # show failure + fix details per attempt
conduit run ... --skip-tests    # apply only; not recommended for real migrations
```

## Tips

- Prefer real unit tests in the consumer repo; generated smoke tests only prove importability.
- For local iteration without burning API quota, leave LLM unset and rely on packet quality + heuristics.
- CI should pass `CONDUIT_LLM_*` secrets only when you want the repair loop online.

## Related docs

- [LLM configuration](llm.md)
- [Pull requests](pull-requests.md)
- [Getting started](getting-started.md)
