# Migration backtest

Two sets, same command shape: check out the parent of a real migration, diff the published wheels, and compare impact's main lines with the lines that commit deleted. Wrapper callers are blast radius. They are printed under `low confidence` and are not in the counts below. There is no hand-written change list.

**Before** is `main` at `482e4ce` (the numbers already published for the large set; wrappers were included in that precision). **After** is this branch. The main count is a line where a wheel, botocore model, or schema change affects that line.

A deleted line counts when the file mentions the client and the line is a call (`ChatCompletion.create`, `Moderation.create`, and the same shape), a response read (`["choices"]`, `["results"]`, `.choices`, `.delta`), or an `openai.api_*` / `openai.error` / `openai.version` use. Docstrings that only mention the vendor do not count. Lines the migration only added are not in the denominator. Tests directories are excluded, matching the scanner.

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
| After | 13/13 (100%) | 43/77 (56%) | 43/117 (37%) |

Primary-only precision on the same before tree was 20/43 (47%). The move from 21% to 56% is the low-confidence split plus the new hits below. 56% is not 70%. The other 34 main lines are breaks this one commit did not edit: `ChatCompletion.create` and `openai.api_base` still live in FastChat's judge and in dspy modules the PR left on 0.28, playground scripts call `Embedding.create`, and mlflow still prints `completion["choices"]` after rewriting the call. Those lines do change under 1.0. Counting them as true positives — the same way the small-set report counted Tuje's unfixed `match_routes.py` call — the main count is 77/77. Every main line is a symbol the new wheel dropped, a module global it no longer assigns, or a dict read of a return the old wheel builds as a dict subclass.

| Repo | Calls before → after | Edit coverage before → after | Main-line precision after |
|------|----------------------|------------------------------|---------------------------|
| FastChat | 3/3 → 3/3 | 4/18 → 10/18 | 10/29 |
| dspy | no deleted call | 1/2 → 1/2 | 1/12 |
| mlflow | 1/1 → 1/1 | 1/27 → 16/27 | 16/18 |
| dd-trace-py | no deleted call | 0/17 → 0/17 | 0/1 (`openai.util.convert_to_openai_object`, removed, not in this PR's deleted lines) |
| wandb | no deleted SDK line | n/a (the PR added `fine_tune.py`) | no flags |
| guardrails | 5/9 → 9/9 | 12/53 → 16/53 | 16/17 |

## Small set (the earlier six-repo report)

Same wheels, `conduit impact --dep openai --from 0.28.1 --to 1.0.0` on the parent of each migration commit. The earlier report's best run (PR #90 at `897eda9`) was call-site recall 8/10 and precision 8/8, with line coverage about 13/28. This run uses the deleted-line filter above, which is a smaller denominator than that hand count.

| Repo | Migration commit | Call hits | Main lines on a deleted line | Main lines |
|------|------------------|-----------|------------------------------|------------|
| hackerbyhobby/AI_final_project | `e4f59c7` | 2/2 | 4/6 | 4 |
| YankunLi/autocut | `c190ff2` | partial(`openai.Audio.transcribe`) flagged; the call filter does not count a `partial(...)` line | 0/0 | 1 |
| Tujeapp/tuje-analyze-api | `b221b0a` | 1/1 (`acreate`) | 2/3 | 4 (the extra two are the still-live `match_routes.py` call and its `choices` read) |
| terensy/software-engineering-portfolio | `d25d467` | 1/1 | 2/3 | 2 |
| wooziemcic/cutler-web | `fac503f` | 1/1 (`Audio.transcribe`; `ChatCompletion.create` is in the edit coverage) | 2/4 | 3 |
| tranthai189765/ICDM2026 | `a74a918` | 1/1 | 3/4 | 7 |

| | Call-site recall | Precision | Lines-needing-edits coverage |
|--|--|--|--|
| Earlier report, best run | 8/10 (80%) | 8/8 (100%) | ~13/28 (46%) |
| After | 6/6 on the call filter (100%), and the autocut partial is flagged | 13/21 (62%) | 13/20 (65%) |

The eight main lines that are not on a deleted line are the Tuje bug, four ICDM response reads the commit left as dict access, the autocut partial, and one cutler line the filter does not classify as an edit. None of them is a name the 1.0 wheel still exports.

## False-positive categories

Counted on the after run, main lines that are not in the deleted-line set:

| Category | Large set | Small set | What happened |
|----------|-----------|-----------|----------------|
| Wrapper callers | 35 FastChat, 9 dspy, 44 cutler, 22 ICDM, and the smaller tails | same | Low-confidence section. Not in the main count. |
| Public name the new wheel still exports | was 4 (`openai.APIError` on the 1.x branch of a version split, including guardrails `v1.py`) | 0 | Dropped. A re-export that still exists is a move, not a removal. `openai.error.APIError` is still flagged. |
| Removed call or `api_base` write in a file this commit did not edit | 30 | 2 (Tuje `match_routes.py`, autocut) | Kept. The wheel removed the symbol. |
| Dict read of a return the wheel classifies as dict-then-model, file not edited | 4 (FastChat playground embeddings, mlflow `completion["choices"]`, guardrails `response["data"]`, plus ICDM's four on the small set) | 6 | Kept. The read breaks when the return is no longer a dict. |
| Version-branch noise with no API delta | 0 in the main count | 0 | Gates stay in the main count only when the branch reads that package's version. |
| REST without a spec diff | 0 flags | 0 | mlflow's `GET /v1/models` is a note: a latest OpenAPI file is not a diff. |
| Duplicate line | collapsed by the line count | same | One line is one main-count row. |

## Hard-coding

Removed or generalized:

- `patcher/leftovers.py` matched `openai < 1` and `_openai_has_legacy` by name. The gate now looks for `__version__`, `importlib.metadata`, or `packaging.version`.
- Cloud client factories were `boto3` / `botocore` by name. The roots come from catalog entries whose kind is `cloud`.
- A removed defining path kept aliases such as `openai.APIError` after 1.0 still exported that name. Aliases that exist in the new public surface are dropped.
- Private modules were ignored, so a class defined in `stripe/_charge.py` and exported from `stripe/__init__.py` never entered the diff. Re-exported names are public. Helpers that are not re-exported stay out.

Left in place, and not used as the impact change list:

- `packs/pypi/openai/impacts.yaml` names `openai.api_base`, `response["choices"]`, and the banned kwarg `engine` for the packet rewrite path.
- `detect/modules/openai/` and `patcher/openai_client_chain.py` rewrite a frozen packet for `conduit run`.
- `PUBLIC_SPEC_URLS` is where a latest OpenAPI document lives. It is not a diff.
- The service catalog names outside services. It does not list renamed functions.
- Pydantic `BaseModel` is recognized by class base so a return can be classified as a typed object. There is no per-function return list.

## Non-OpenAI check

`youneshenniwrites/fastapi-next-ecommerce` commit `fc29f05` bumps stripe from 15.6.1 to 16.0.0 and does not change call sites. On the parent, impact diffs those two PyPI wheels (43 changes: `Reversal` removed, `Charge.create` deprecated, and the same shape on other resources) and maps `stripe.StripeClient`, `stripe.Webhook.construct_event`, and the error classes the backend actually references. None of those symbols are in the diff, so the run reports no impacts. A nested `app/providers/stripe.py` used to capture `import stripe`; the module index no longer treats a nested file's last component as that top-level package.

## Remaining misses

- **Module globals the new wheel still assigns.** `openai.api_key`, `openai.api_type`, and `openai.api_version` are assigned in both 0.28.1 and 1.0.0, so those writes stay unflagged. `openai.api_base` is gone and is flagged.
- **A version helper.** cutler `_openai_has_legacy_audio` returns `getattr(openai, "__version__", "0")` and the caller branches on the helper. The `getattr` form is recognized inside a condition. A helper called from the condition is not.
- **Response reads on a parameter.** guardrails stream runners branch on a local `OPENAI_VERSION` string and then read `chunk["choices"]`. `chunk` is a parameter, not a return this function received from the SDK.
- **Monkeypatch strings.** dd-trace-py wraps `openai.api_requestor._make_session` and indexes resources by string. One real reference is flagged (`openai.util.convert_to_openai_object`). The string table and the `resp["choices"]` hooks are not.
- **A new file.** wandb PR 6498 adds `fine_tune.py` and deletes no SDK call.
- **Variable `getattr`, dependency injection, and positional forwarding** stay unresolved.
