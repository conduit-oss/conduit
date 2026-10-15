# Migration backtest

`conduit-realtest/REPORT.md` is not in this checkout or in git history. The numbers below are a fresh run in that style: check out the commit before a merged migration, diff the wheels for real, and compare the lines impact flags with the lines the migration deleted.

The six repositories are public OpenAI 0.28 to 1.x migrations, including the FastChat commit named in [Outside-in mode](outside-in.md). Each parent was compared with `openai` 0.28.1 against 1.0.0. Both wheels were downloaded from PyPI. There is no hand-written change list.

| Repo | PR | Parent | Merge |
|------|----|--------|-------|
| [lm-sys/FastChat](https://github.com/lm-sys/FastChat/pull/3032) | 3032 | `98b8f642` | `e087d6e8` |
| [stanfordnlp/dspy](https://github.com/stanfordnlp/dspy/pull/403) | 403 | `24865c1e` | `889f7fa8` |
| [mlflow/mlflow](https://github.com/mlflow/mlflow/pull/11034) | 11034 | `2f692895` | `c1c4243d` |
| [DataDog/dd-trace-py](https://github.com/DataDog/dd-trace-py/pull/7513) | 7513 | `b4eb548f` | `eb220c96` |
| [wandb/wandb](https://github.com/wandb/wandb/pull/6498) | 6498 | `7a705a9b` | `afffbe60` |
| [guardrails-ai/guardrails](https://github.com/guardrails-ai/guardrails/pull/788) | 788 | `4386c362` | `a28d01c1` |

**Before** is `main` at `482e4ce`. **After** is this branch. Tests directories are excluded, matching the scanner.

A deleted line counts when the file mentions the OpenAI client and the line is a call (`ChatCompletion.create`, `Moderation.create`, `client.chat.completions.create`, and the same shape), a response read (`["choices"]`, `["results"]`, `.choices`, `.delta`), or an `openai.api_*` / `openai.error` / `openai.version` use. Docstrings that merely say "OpenAI" do not count. Lines that the migration only added are not on the parent tree, so they are not in the denominator.

| | Call-site recall | Precision | Lines-needing-edits coverage |
|--|--|--|--|
| Before | 9/13 (69%) | 18/85 (21%) | 18/117 (15%) |
| After | 9/13 (69%) | 20/87 (23%) | 20/117 (17%) |

Precision is every flagged line in the repo against lines this one PR deleted. A repo that still has other `ChatCompletion.create` calls, which this PR did not touch, lowers that number. File precision below keeps only flags inside files the PR edited.

| Repo | Calls before → after | Edit coverage before → after | File precision before → after |
|------|----------------------|------------------------------|-------------------------------|
| FastChat | 3/3 → 3/3 | 4/18 (22%) → 6/18 (33%) | 4/6 (67%) → 6/8 (75%) |
| dspy | no deleted call | 1/2 → 1/2 | 1/14 → 1/14 |
| mlflow | 1/1 → 1/1 | 1/27 → 1/27 | 1/3 → 1/3 |
| dd-trace-py | no deleted call | 0/17 → 0/17 | no flags |
| wandb | no deleted SDK line | n/a (the PR added `fine_tune.py`) | no flags |
| guardrails | 5/9 → 5/9 | 12/53 (23%) → 12/53 (23%) | 12/12 → 12/12 |

FastChat is the move. The two new hits are the stream reads `chunk["choices"]` and `chunk["choices"][0]["delta"].get(...)`, which the parent loop binds from `openai.ChatCompletion.create`. The wheel diff is what marks that return as dict-to-model. Repo-wide precision stays low because FastChat has 51 other call sites this PR left alone.

This is not the unpublished ~46% figure. On this denominator the edit coverage is 15% then 17%. The gap is the misses below, not a different wheel.

## Remaining misses

- **Module attribute writes.** FastChat still assigns `openai.api_base`, `openai.api_key`, `openai.api_type`, and `openai.api_version`. Those names are module globals in the 0.28 wheel, not callables, so the wheel diff does not list them and the lines stay unflagged.
- **Returns whose body does not look like a dict.** `openai.Moderation.create` is flagged as removed. `res["results"][0]["flagged"]` in FastChat is not, because the 0.28 function body is not classified as returning a dict-like object. `ChatCompletion.create` is, and its stream reads are flagged.
- **A callable passed around, not called.** guardrails does `return openai.ChatCompletion.create` and `return openai.Completion.acreate`. The line loads the removed callable and does not call it. Four of the nine deleted call lines are that shape.
- **A version string that is not the library attribute.** guardrails branches on `OPENAI_VERSION.startswith("0")` and then reads `chunk["choices"]`. `chunk` is a parameter. The branch is not `openai.__version__`, `importlib.metadata.version`, or `packaging.version`, so it is not a version gate, and the read is not tied to an SDK call in that function.
- **Monkeypatch, not a call.** dd-trace-py edits `ddtrace/contrib/openai/patch.py` and endpoint hooks. The parent has no direct `openai.*.create` call in those deleted lines, so impact flags nothing there.
- **A new file.** wandb PR 6498 adds `wandb/integration/openai/fine_tune.py` (408 lines) and deletes no SDK call. Impact on the parent has nothing in that file to flag.
- **Most mlflow example edits** are `openai.api_key` / `openai.api_base` writes and chat examples whose response reads are not on the same assigned return that the wheel classified as a shape change. One `ChatCompletion` call is flagged.
- **Variable `getattr`, dependency injection, and positional forwarding** stay unresolved. `examples/wrapped-openai-fixture/dynamic_gap.py` is the fixture for that.
- **REST without two specs.** mlflow's `GET https://api.openai.com/v1/models` calls are mapped. The note says they were not compared: the public OpenAPI document is a single latest file, and a latest file is not a diff.
