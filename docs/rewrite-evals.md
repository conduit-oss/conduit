# Rewrite evals

How Conduit scores the **apply / rewrite** path without waiting on 45-minute
hosted workflow runs.

The suite lives in [`evals/rewrite/`](../evals/rewrite/). Read
[`evals/rewrite/README.md`](../evals/rewrite/README.md) for commands.

## What it measures

Each labeled case is one rewrite input (frozen files + impact JSON + recorded
model responses) and the expected `SiteStatus` / file constraints. The runner
calls `apply_sites` and grades with code:

- expected status (verified, needs-human, skipped, needs review, …)
- no false keeps
- optional file contents and context-pack checks

## Lanes

| Lane | When | Command |
|------|------|---------|
| Smoke | Every PR | `python evals/rewrite/run.py --smoke` |
| Full | Prompt/model changes | `python evals/rewrite/run.py --full` |

Smoke must clear `smoke_threshold` (default 88%) with zero false keeps. Full
must clear `accuracy_floor` (default 90%).

## Agreement with the eval plan

- Minimum viable labeled set (20 cases) before a large harness
- Code graders before any LLM judge
- Accuracy floor, then cost-per-completed-task when comparing models
- Prompt iteration first; model upgrade only when the score stops moving
- CI smoke required; full suite and production mining grow the set over time
