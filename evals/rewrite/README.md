# Rewrite evals

Minimum viable eval for the **rewrite / `conduit apply`** path.

Labeled answers are written in `catalog.json` before any model run. The runner
grades with **code checks** (status, splice side-effects, coverage/review
gates). Recorded `FakeModelClient` responses keep the PR smoke lane fast.

This follows the Claude Academy / OpenAI eval methodology adapted to Conduit:

1. Task-specific cases that look like apply traffic
2. Code graders first; no LLM judge in the MVP
3. Accuracy floor before model/cost comparisons
4. Prompt iteration before upgrading the propose model
5. CI smoke on every PR; full suite on demand / schedule

## Quick start

```bash
pip install -e "./conduit[dev]"
python evals/rewrite/run.py --smoke
python evals/rewrite/run.py --full
python evals/rewrite/run.py --case typical-rename-verified
```

Exit code `0` only when accuracy clears the threshold and false keeps stay at
or under `max_false_keeps`.

| Lane | Flag | Threshold field | Intent |
|------|------|-----------------|--------|
| PR smoke | `--smoke` (default) | `smoke_threshold` (0.88) | Required CI check, every category represented |
| Full suite | `--full` | `accuracy_floor` (0.90) | Prompt/model changes |
| One case | `--case <id>` | n/a (reports only) | Debug a failure |

## Catalog shape

Each case in `catalog.json` has:

- `id`, `category`, `description`
- `files` — frozen consumer tree
- `impact` — impact JSON payload
- `responses` — recorded propose/summarize queue for smoke/full offline runs
- `site_cleared` — clear-check recipe (`always`, `never`, `new_without_old`, …)
- `test_cmd` — `exercise`, a shell command, or `null`
- `expected` — labeled answers (statuses, file contents, why text, …)

Categories: `typical`, `dry-run`, `needs-human`, `needs-review`, `coverage`,
`multi-site`, `wrapper`.

## Metrics

- **Accuracy** — share of cases whose code grade passes
- **False keeps** — any `fixed*` status when the label did not expect a keep
- **Cost per completed task (tokens)** — `tokens_used / passed_cases` (proxy until live Token Factory runs)

When comparing live models later: set an accuracy floor from the cost of a bad
rewrite, drop models below it, then pick the lowest cost per completed task.
Iterate the apply prompt before upgrading Nemotron Ultra.

## Production loop

1. PR smoke (this runner) blocks merges when below threshold
2. Full suite on prompt/model edits (`--full`, optionally `--repeats 2`)
3. Mine hosted apply failures into new labeled cases (drift)

Live-model mode is intentionally not in the MVP runner. Add it only after the
recorded suite is green and stable.
