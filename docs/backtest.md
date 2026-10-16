# Backtest

Same check each time: start from the parent of a real migration, see which lines `conduit impact` flags, and compare those lines with the lines that commit deleted. Wrapper callers are listed separately and are not in these counts.

The fractions below are unchanged from the last scored checks. Specs are discovered from a package's own source repository at the two release tags. There is no location table of product names.

## Large set (six merged OpenAI 0.28 → 1.x migrations)

| Repo | PR | Parent | Merge |
|------|----|--------|-------|
| [lm-sys/FastChat](https://github.com/lm-sys/FastChat/pull/3032) | 3032 | `98b8f642` | `e087d6e8` |
| [stanfordnlp/dspy](https://github.com/stanfordnlp/dspy/pull/403) | 403 | `24865c1e` | `889f7fa8` |
| [mlflow/mlflow](https://github.com/mlflow/mlflow/pull/11034) | 11034 | `2f692895` | `c1c4243d` |
| [DataDog/dd-trace-py](https://github.com/DataDog/dd-trace-py/pull/7513) | 7513 | `b4eb548f` | `eb220c96` |
| [wandb/wandb](https://github.com/wandb/wandb/pull/6498) | 6498 | `7a705a9b` | `afffbe60` |
| [guardrails-ai/guardrails](https://github.com/guardrails-ai/guardrails/pull/788) | 788 | `4386c362` | `a28d01c1` |

Wheels: openai 0.28.1 and 1.0.0 from PyPI.

| | Call-site recall | Precision (main lines ∩ deleted lines) | Lines-needing-edits coverage |
|--|--|--|--|
| Before | 9/13 (69%) | 18/85 (21%), wrappers included | 18/117 (15%) |
| After | 13/13 (100%) | 43/79 (54%) | 43/117 (37%) |

Primary-only precision on the same before tree was 20/43 (47%). A later check after return-class plumbing kept call-site recall at 13/13 and edit coverage at 43/117. The main-line denominator is 79. 54% is the strict overlap with lines that one commit deleted. Counting every main line that is a symbol the new wheel dropped, a global it no longer assigns, or a dict read of a return that became a typed object, the main count is 79/79.

| Repo | Calls before → after | Edit coverage before → after | Main-line precision after |
|------|----------------------|------------------------------|---------------------------|
| FastChat | 3/3 → 3/3 | 4/18 → 10/18 | 10/29 |
| dspy | no deleted call | 1/2 → 1/2 | 1/14 |
| mlflow | 1/1 → 1/1 | 1/27 → 16/27 | 16/18 |
| dd-trace-py | no deleted call | 0/17 → 0/17 | 0/1 (`openai.util.convert_to_openai_object`, removed, not in this PR's deleted lines) |
| wandb | no deleted SDK line | n/a (the PR added `fine_tune.py`) | no flags |
| guardrails | 5/9 → 9/9 | 12/53 → 16/53 | 16/17 |

## Small set

Same wheels, `conduit impact --dep openai --from 0.28.1 --to 1.0.0` on the parent of each migration commit.

| Repo | Migration commit | Call hits | Main lines on a deleted line | Main lines |
|------|------------------|-----------|------------------------------|------------|
| hackerbyhobby/AI_final_project | `e4f59c7` | 2/2 | 4/6 | 4 |
| YankunLi/autocut | `c190ff2` | partial(`openai.Audio.transcribe`) flagged; the call filter does not count a `partial(...)` line | 0/0 | 2 |
| Tujeapp/tuje-analyze-api | `b221b0a` | 1/1 (`acreate`) | 2/3 | 4 (the extra two are the still-live `match_routes.py` call and its `choices` read) |
| terensy/software-engineering-portfolio | `d25d467` | 1/1 | 2/3 | 2 |
| wooziemcic/cutler-web | `fac503f` | 1/1 (`Audio.transcribe`; `ChatCompletion.create` is in the edit coverage) | 2/4 | 3 |
| tranthai189765/ICDM2026 | `a74a918` | 1/1 | 3/4 | 7 |

| | Call-site recall | Precision | Lines-needing-edits coverage |
|--|--|--|--|
| Earlier report, best score | 8/10 (80%) | 8/8 (100%) | ~13/28 (46%) |
| After | 6/6 on the call filter (100%), and the autocut partial is flagged | 13/22 (59%) | 13/20 (65%) |

Concrete hit rate on that set: 22/22.

## Other clients

No product was added by name for these scores.

`youneshenniwrites/fastapi-next-ecommerce` commit `fc29f05` bumps stripe from 15.6.1 to 16.0.0 and does not change call sites. Impact reports no line hits: the symbols that repo uses are still in the new wheel.

| Repo | Commit | Wheels | Call-site recall | Strict precision | Concrete | Edit coverage |
|------|--------|--------|------------------|------------------|----------|---------------|
| [ConesaLab/PaintOmics](https://github.com/ConesaLab/PaintOmics/commit/0dcc014a306d1b3bfa2f8887c1156a249aaa7edd) | `0dcc014` parent `a039d5d8` | pymongo 3.13.0 → 4.10.1 | 27/27 | 27/28 | 28/28 | 27/27 |
| [YliasR/briehost-api](https://github.com/YliasR/briehost-api/commit/daf9c0b3562d41311a612b49e10f1334b80f1212) | `daf9c0b` parent `f458b815` | stripe 14.4.1 → 15.0.0 | 1/1 | 1/1 | 1/1 | 1/1 |
| [aaronsb/knowledge-graph-system](https://github.com/aaronsb/knowledge-graph-system/commit/b97601e082ca9012809d185353cbb23cad9ecb6d) | `b97601e` parent `4aebdb32` | anthropic 0.125.0 → 1.0.0 | 4/5 (was 0/5) | 4/4 (was 0/4) | 4/4 | 4/5 (was 0/5) |
| [great-expectations/great_expectations](https://github.com/great-expectations/great_expectations/pull/7469) | `0fe0cfe` parent `da817659` | SQLAlchemy 1.4.54 → 2.0.0 | 6/10 | 6/19 | 9/19 | 6/10 |
| **Total** | | | **38/43 (88%)** (was 34/43) | **38/52 (73%)** (was 34/52) | **42/52 (81%)** | **38/43 (88%)** (was 34/43) |

OpenAI large and small sets on the same matcher: large 13/13, 43/79 (54%), 43/117; small 6/6 on the call filter, 13/22 (59%), 13/20.

PaintOmics: all 27 deleted calls are flagged. The 28th main line is `PathwayAcquisitionJobDAO.py:83`, which the commit left in place.

briehost: one deleted line, one flag.

knowledge-graph-system: four deleted assignments are flagged. `golden_describe_image.py:65` is still a miss. The client there is a stand-in, so there is no real call chain in that file.

great_expectations: six of ten deleted `.execute(` lines are flagged. Four are misses because the receiver is a parameter. The other 13 main lines were not deleted by that pull request.

### Pinecone

[gigas5234/dys-backend](https://github.com/gigas5234/dys-backend/commit/8934c9ea2c3158d43ac4419960f82568ece03b00) `8934c9ea` (parent `c102674`) deletes `pinecone.init`, `pinecone.list_indexes`, `pinecone.create_index`, and `pinecone.Index`. This was not scored above. The wheels named `pinecone` at 2.2.4 and 3.0.0 are 2.5 KB and 4.4 KB. The old surface lives in the `pinecone-client` distribution (2.2.4 is 179 KB, 3.0.0 is 200 KB). A same-name wheel diff cannot see an API that moved to another distribution.

## REST specs

| Repo | Commit | Command | Call-site recall | Precision | Edit coverage |
|------|--------|---------|------------------|-----------|---------------|
| [kikuchi-mizuki/lp](https://github.com/kikuchi-mizuki/lp/commit/c519e1ca4249da5398492e0d026ac5223ae48784) | `c519e1c` parent `3d85dcc` | `conduit impact --dep stripe --from v1000 --to v2565` | 1/1 | 1/1 | 1/1 |
| [betterchoicedev/NewClientsWeb](https://github.com/betterchoicedev/NewClientsWeb/commit/32aa86ff2f4c318befcb3eac21adff44d3f45dfd) | `32aa86f` parent `32aa86f^` | same | 1/1 | 1/1 | 1/1 |
| [dpup/git-workflow](https://github.com/dpup/git-workflow/commit/3b34e962a966550bc5e96f9b5bb7e0e58befd0d4) | `3b34e96` parent `ba7c875` | `--dep github --from 2021-06-10 --to 2023-02-16` | 2/2 | 2/2 | |
| [apache/airflow-client-python](https://github.com/apache/airflow-client-python/commit/7a2ccc6629c39fa8559cbfc0b60514655d522705) | `7a2ccc6` parent `241405e` | `--dep airflow --from 3.0.0 --to 3.1.3` | 1/1 | 1/1 | |

The github pair (2/2) was scored with a registry fallback that has since been removed. Specs now come from the package's own source repository. The airflow pair uses the committed spec at the client tags (`1/1`). Stripe REST stayed 2/2. OpenAI and non-OpenAI wheel sets were re-checked after that removal: large 13/13, 43/79 (54%), 43/117; small 6/6, 13/22 (59%), 13/20; non-OpenAI 38/43 (88%), 38/52 (73%).

## After evidence classification

A dependency is an outside service only when its own files show network or database I/O, or ship a versioned API spec, and the caller builds a client. Re-scored against `main` at `1ed1106`. Primary lines are identical, so recall and strict precision do not move.

| Set | Before (`main` `1ed1106`) | After |
|-----|---------------------------|-------|
| OpenAI large | 13/13 call sites, 43/79 strict (54%), 43/117 edit coverage | same |
| OpenAI small | 6/6 on the call filter, 13/22 strict (59%), 13/20 edit coverage | same |
| Non-OpenAI | 38/43 caught (88%), 38/52 strict (73%) | same |

Primary-line counts, before and after: FastChat 29, dspy 14, mlflow 18, dd-trace-py 1, wandb 0, guardrails 17 (79); small set 4, 2, 4, 2, 3, 7 (22); PaintOmics 28, briehost 1, knowledge-graph-system 4, great_expectations 19 (52).

## Still missed

- `openai.api_key`, `openai.api_type`, and `openai.api_version` are assigned in both 0.28.1 and 1.0.0, so those writes stay unflagged. `openai.api_base` is gone and is flagged.
- A version helper called from a condition is not treated as a version check. `getattr` inside the condition is.
- Response reads on a parameter are not tied back to the client.
- String tables and monkeypatched hooks are not call sites.
- wandb's pull request adds a file and deletes no SDK call.
- A removed parameter with no client call in the file is not flagged.
- A method on a parameter (`execution_engine.execute`) is not flagged.
- An API that moved to another distribution (the pinecone case above) is not visible from the stub wheel.
