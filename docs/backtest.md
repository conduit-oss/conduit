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
| After | 13/13 (100%) | 43/79 (54%) | 43/117 (37%) |

Primary-only precision on the same before tree was 20/43 (47%). The move from 21% to 54% is the low-confidence split plus the new hits below. A re-run after the return-class plumbing below kept call-site recall at 13/13 and edit coverage at 43/117. The main-line denominator moved from 77 to 79: dspy gained two `openai.api_base` writes under `testing/tasks/` that PR 403 did not delete. 54% is the strict overlap with lines that one commit deleted. The other 36 main lines are breaks this one commit did not edit: `ChatCompletion.create` and `openai.api_base` still live in FastChat's judge and in dspy modules the PR left on 0.28, playground scripts call `Embedding.create`, and mlflow still prints `completion["choices"]` after rewriting the call. Those lines do change under 1.0. Counting them as true positives — the same way the small-set report counted Tuje's unfixed `match_routes.py` call — the main count is 79/79. Every main line is a symbol the new wheel dropped, a module global it no longer assigns, or a dict read of a return the old wheel builds as a dict subclass.

| Repo | Calls before → after | Edit coverage before → after | Main-line precision after |
|------|----------------------|------------------------------|---------------------------|
| FastChat | 3/3 → 3/3 | 4/18 → 10/18 | 10/29 |
| dspy | no deleted call | 1/2 → 1/2 | 1/14 |
| mlflow | 1/1 → 1/1 | 1/27 → 16/27 | 16/18 |
| dd-trace-py | no deleted call | 0/17 → 0/17 | 0/1 (`openai.util.convert_to_openai_object`, removed, not in this PR's deleted lines) |
| wandb | no deleted SDK line | n/a (the PR added `fine_tune.py`) | no flags |
| guardrails | 5/9 → 9/9 | 12/53 → 16/53 | 16/17 |

## Small set (the earlier six-repo report)

Same wheels, `conduit impact --dep openai --from 0.28.1 --to 1.0.0` on the parent of each migration commit. The earlier report's best run (PR #90 at `897eda9`) was call-site recall 8/10 and precision 8/8, with line coverage about 13/28. This run uses the deleted-line filter above, which is a smaller denominator than that hand count.

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
| Earlier report, best run | 8/10 (80%) | 8/8 (100%) | ~13/28 (46%) |
| After | 6/6 on the call filter (100%), and the autocut partial is flagged | 13/22 (59%) | 13/20 (65%) |

The nine main lines that are not on a deleted line are the Tuje bug, four ICDM response reads the commit left as dict access, the autocut partial, the `self.whisper_model(...)` call that partial feeds, and one cutler line the filter does not classify as an edit. Every one of them is a name the 1.0 wheel dropped. Concrete hit rate 22/22.

## False-positive categories

Counted on the after run, main lines that are not in the deleted-line set:

| Category | Large set | Small set | What happened |
|----------|-----------|-----------|----------------|
| Wrapper callers | 35 FastChat, 9 dspy, 44 cutler, 22 ICDM, and the smaller tails | same | Low-confidence section. Not in the main count. |
| Public name the new wheel still exports | was 4 (`openai.APIError` on the 1.x branch of a version split, including guardrails `v1.py`) | 0 | Dropped. A re-export that still exists is a move, not a removal. `openai.error.APIError` is still flagged. |
| Removed call or `api_base` write in a file this commit did not edit | 32 | 3 (Tuje `match_routes.py`, autocut partial, autocut `self.whisper_model(...)`) | Kept. The wheel removed the symbol. |
| Dict read of a return the wheel classifies as dict-then-model, file not edited | 4 (FastChat playground embeddings, mlflow `completion["choices"]`, guardrails `response["data"]`, plus ICDM's four on the small set) | 6 | Kept. The read breaks when the return is no longer a dict. |
| Version-branch noise with no API delta | 0 in the main count | 0 | Gates stay in the main count only when the branch reads that package's version. |
| REST without a versioned OpenAPI pair | 0 flags | 0 | mlflow's `GET /v1/models` is a note when `--from`/`--to` cannot pick two documents. |
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
- `SPEC_SOURCES` maps a host to where versioned OpenAPI history lives (git repo path, Stainless `.stats.yml`, Speakeasy/OpenAPI Generator SDK repos, APIs.guru, Azure, Google discovery). It is a location catalog, not a list of endpoint changes. Generic discovery also walks PyPI `project_urls` → release tags for committed specs.
- The service catalog names outside services. It does not list renamed functions.
- Pydantic `BaseModel` is recognized by class base so a return can be classified as a typed object. There is no per-function return list.
- A function's return class is copied onto each public alias of that function, so `pkg.Client.method` and `pkg._mod.Client.method` share it. `class Child(Base["T"])` still inherits from `Base`. A method the child does not define resolves to the base that does. `factory = kwargs.pop(key, Engine); return factory()` constructs `Engine`.
- A field assigned on a base class is visible on the subclass. `x = None` after a failed import does not hide the import. A module that lives under `app/src/...` is also indexed as `src....` when that shorter name is unique.
- The runner keeps the wheel's return classes, inherited methods, and still-exported names on the change list. `list.extend` had been dropping them.
- When an SDK call passes `**kwargs` / `**dict` and the wheel removed or renamed a parameter, the introducing lines are primary findings: `d["key"] = ...`, `d.update(...)`, `dict(...)`, `{"key": ...}`, `d.setdefault(...)`, keywords on `functools.partial`, and keys returned from a simple local helper. The call line stays in the expression as context. No package name is special-cased.

## Non-OpenAI check

Same generic matcher. No package was added to the catalog for this run, and no rule names pymongo, stripe, anthropic, sqlalchemy, or pinecone.

`youneshenniwrites/fastapi-next-ecommerce` commit `fc29f05` bumps stripe from 15.6.1 to 16.0.0 and does not change call sites. On the parent, impact diffs those two PyPI wheels (43 changes: `Reversal` removed, `Charge.create` deprecated, and the same shape on other resources) and maps `stripe.StripeClient`, `stripe.Webhook.construct_event`, and the error classes the backend actually references. Those symbols are absent from that diff, so the run reports no impacts. A nested `app/providers/stripe.py` used to capture `import stripe`; the module index no longer treats a nested file's last component as that top-level package.

### Migrations where call sites changed

Each repo is the parent of a merged commit. Wheels are the real from/to distributions. A call site is a deleted non-comment, non-test line that is the SDK call the migration rewrote (`.insert` / `.update` / `.remove`, `to_dict_recursive(`, a `temperature` write into the request dict, or `.execute(`). Strict precision is main lines that sit on one of those deleted lines. Concrete precision is main lines whose change is a symbol, parameter, or module global the new wheel dropped. Version branches stay in the main count and are called out under concrete.

| Repo | Commit | Wheels | Call-site recall | Strict precision | Concrete | Edit coverage |
|------|--------|--------|------------------|------------------|----------|---------------|
| [ConesaLab/PaintOmics](https://github.com/ConesaLab/PaintOmics/commit/0dcc014a306d1b3bfa2f8887c1156a249aaa7edd) | `0dcc014` parent `a039d5d8` | pymongo 3.13.0 → 4.10.1 | 27/27 | 27/28 | 28/28 | 27/27 |
| [YliasR/briehost-api](https://github.com/YliasR/briehost-api/commit/daf9c0b3562d41311a612b49e10f1334b80f1212) | `daf9c0b` parent `f458b815` | stripe 14.4.1 → 15.0.0 | 1/1 | 1/1 | 1/1 | 1/1 |
| [aaronsb/knowledge-graph-system](https://github.com/aaronsb/knowledge-graph-system/commit/b97601e082ca9012809d185353cbb23cad9ecb6d) | `b97601e` parent `4aebdb32` | anthropic 0.125.0 → 1.0.0 | 4/5 (was 0/5) | 4/4 (was 0/4) | 4/4 | 4/5 (was 0/5) |
| [great-expectations/great_expectations](https://github.com/great-expectations/great_expectations/pull/7469) | `0fe0cfe` parent `da817659` | SQLAlchemy 1.4.54 → 2.0.0 | 6/10 | 6/19 | 9/19 | 6/10 |
| **Total** | | | **38/43 (88%)** (was 34/43) | **38/52 (73%)** (was 34/52) | **42/52 (81%)** | **38/43 (88%)** (was 34/43) |

OpenAI large and small sets were re-run on the same matcher after kwargs-origin tracing. Call-site recall, edit coverage, and primary counts are unchanged: large 13/13, 43/79 (54%), 43/117; small 6/6 on the call filter, 13/22 (59%), 13/20.

PaintOmics rewrote `collection.insert` / `update` / `remove` after `db[name][collection]`. All 27 deleted calls are flagged. The 28th main line is `PathwayAcquisitionJobDAO.py:83`, `getCollection("counters").update(...)`, which the commit left in place. `insert_one` and `find` still exist in 4.10.1 and are not flagged. `connection.execute` after `engine.begin()` in great_expectations is not flagged; `Engine.execute` is.

briehost deletes `event.to_dict_recursive()` at `app/payments/stripe.py:156` after `Webhook.construct_event`. That line is the one flag. The wheel removed `StripeObject.to_dict_recursive`.

knowledge-graph-system moves `temperature` off `messages.create` and into `extra_body`. The four deleted assignments in `ai_providers.py` (lines 1272, 1355, 1435, 1519) sit one to three lines above `self.client.messages.create(**request_kwargs)`. Kwargs-origin tracing flags those four assignment lines as primary findings; the call stays in the expression as context (`request_kwargs['temperature'] → … messages.create`). `scripts/development/diagnostics/golden_describe_image.py:65` still misses: it writes `temperature` into a dict returned for a comparison, and the client there is a `MagicMock`, so there is no `messages.create` chain in that file.

great_expectations PR 7469 deletes 10 `.execute(` lines while rewriting SQLAlchemy 2 use. Six are flagged: `database_store_backend.py` 263, 330, 350, 362; `query_store.py:126`; `sqlalchemy_execution_engine.py:1147`. Four are misses because the receiver is a parameter: `map_condition_auxilliary_methods.py:365` (`execution_engine.engine.execute`) and `expectations/metrics/util.py` 504, 589, 611 (`sqlalchemy_engine.execute`). The other 13 main lines are real and were not deleted by this PR: `sqlalchemy_execution_engine.py:1061` and `assets/partners/anthonydb/just_connect.py:5` still call `Engine.execute`, `core/util.py:78` imports removed `LegacyRow`, and 10 lines branch on `sqlalchemy.__version__`. Concrete 9/19 counts the removed `Engine.execute` and `LegacyRow` lines. The 10 version branches are in the main count because they read that package's version; they are not a removed symbol.

### Pinecone

[gigas5234/dys-backend](https://github.com/gigas5234/dys-backend/commit/8934c9ea2c3158d43ac4419960f82568ece03b00) `8934c9ea` (parent `c102674`) deletes `pinecone.init`, `pinecone.list_indexes`, `pinecone.create_index`, and `pinecone.Index` in `src/backend/database/pinecone_client.py`. This is a miss, and it was not scored above. `pinecone` is not an outside service in the catalog, so `conduit impact --dep pinecone` has nothing to diff. The PyPI wheels named `pinecone` at 2.2.4 and 3.0.0 are 2.5 KB and 4.4 KB. The v2 surface lives in the `pinecone-client` distribution (2.2.4 is 179 KB, 3.0.0 is 200 KB). A same-name wheel diff cannot see an API that moved to another distribution. No catalog entry was added for this repo.

## Versioned OpenAPI REST

Raw `requests` / `httpx` / `aiohttp` / `urllib` calls (and vendor SDK helpers that take an OpenAPI path string, such as `stripe.*._static_request`, OpenAPI Generator `param_serialize(resource_path=…)`, Speakeasy `path="/…"`) are mapped by host and path. Change data comes from diffing two resolved OpenAPI / Swagger / Google discovery documents — not from a hand-written endpoint list.

### Generic discovery rule

Given an SDK package: PyPI `project_urls` → GitHub source repo → at each release tag look for a committed OpenAPI/Swagger/discovery document (common paths `openapi.{yaml,json}`, `spec/`, `specs/`, `api/`, `.speakeasy/` workflow inputs, OpenAPI Generator inputs, `.stats.yml` `openapi_spec_url`). Diff the from-tag vs to-tag documents and map onto REST call sites. Public registries with git history are also supported: APIs.guru `openapi-directory`, Azure `azure-rest-api-specs`, Google `discovery-artifact-manager`.

### Spec source verification (live repos)

| Source | Works? | Concrete example | Notes |
|--------|--------|------------------|-------|
| Stainless `.stats.yml` | Partial | `anthropics/anthropic-sdk-python` tags through `v1.0.0` expose `openapi_spec_url`; `v1.5.0+` and `cloudflare/cloudflare-python` tip do not | Honest miss when URL absent |
| Speakeasy workflow | Yes when pinned | `clerk/clerk-sdk-python` `@v1.8.0` → `clerk/openapi-specs` `bapi/2024-10-01.yml`; `@v3.0.0` → `bapi/2025-03-12.yml` | `gr4vy/gr4vy-python` points at an unversioned `gr4vy.github.io` URL — same tip at every tag, not a from/to pair |
| Fern-generated SDK | No | `cohere-ai/cohere-python`, `VapiAI/server-sdk-python` ship `.fern/metadata.json` only | API definitions live elsewhere; discovery reports Fern without a committed spec |
| OpenAPI Generator | Yes | `apache/airflow-client-python` tags ship `spec/v2.yaml` (`3.0.0` → `3.1.3` removes `GET /api/v2/dagReports`) | Also maps `param_serialize(resource_path=…)` call sites |
| APIs.guru directory | Yes (sparse) | `APIs/github.com/api.github.com/1.1.4/openapi.yaml` commits `2021-06-09` → `2023-02-15` remove `/authorizations` | Prefer first-party repos when they have denser history; used as github fallback |
| Azure rest-api-specs | Yes | `specification/storage/…/stable/{2023-05-01,2026-09-01}/openapi.json` | TypeSpec layout (`openapi.json` under `stable/`); recursive git trees are truncated — walk contents API |
| Google discovery | Yes | `discoveries/drive.v3.json` / `gmail.v1.json` by commit date | No release tags; `flatPath` normalization avoids false removals from `videos` → `youtube/v3/videos` path reshapes |

Catalogued shortcuts (not change lists):

| Service | Kind | Location | What actually exists |
|---------|------|----------|----------------------|
| stripe | git_repo | `stripe/openapi` `openapi/spec3.json` | Tagged releases (`vNNNN`). |
| github | git_repo (+ APIs.guru fallback) | `github/rest-api-description` … | Continuous commits; resolve by date; falls back to APIs.guru when the primary path has no history for the refs. |
| openai | git_repo | `openai/openai-openapi` `openapi.yaml` | Sparse tags (`1.3.0`, `2.0.0`). |
| anthropic | stainless_sdk | `anthropics/anthropic-sdk-python` `.stats.yml` | See Stainless row above. |
| cloudflare | stainless_sdk | `cloudflare/cloudflare-python` `.stats.yml` | No `openapi_spec_url` on current tags. |
| airflow | sdk_repo | `apache/airflow-client-python` | Discover `spec/v2.yaml` at tags. |
| clerk | sdk_repo | `clerk/clerk-sdk-python` | Speakeasy workflow → dated openapi-specs. |
| azure-storage / azure-web | azure_specs | `Azure/azure-rest-api-specs` | Version dirs or commit-by-date. |
| google-drive / google-gmail | google_discovery | `googleapis/discovery-artifact-manager` | Commit-by-date. |
| apis-guru-github / apis-guru-stripe | apis_guru | `APIs-guru/openapi-directory` | Version dirs or commit-by-date. |

When no from/to pair can be resolved, impact keeps an honest `not compared: …` note.

### REST migration backtests

Same command shape: check out the parent, run `conduit impact --dep <svc> --from <ref> --to <ref>`. A call site is a deleted non-comment line that is the REST call (or SDK path request) the migration rewrote. Strict precision is main lines on those deleted lines.

#### Stripe OpenAPI tags (existing)

`conduit impact --dep stripe --from v1000 --to v2565` (removes `POST …/usage_records` and `GET /v1/invoices/upcoming`).

| Repo | Commit | Call-site recall | Precision | Edit coverage |
|------|--------|------------------|-----------|---------------|
| [kikuchi-mizuki/lp](https://github.com/kikuchi-mizuki/lp/commit/c519e1ca4249da5398492e0d026ac5223ae48784) | `c519e1c` parent `3d85dcc` | 1/1 | 1/1 | 1/1 |
| [betterchoicedev/NewClientsWeb](https://github.com/betterchoicedev/NewClientsWeb/commit/32aa86ff2f4c318befcb3eac21adff44d3f45dfd) | `32aa86f` parent `32aa86f^` | 1/1 | 1/1 | 1/1 |

lp deletes `requests.post(…/usage_records)` at `lp/services/line_service.py:523`. NewClientsWeb deletes the path on `stripe.SubscriptionItem._static_request(…, "/v1/subscription_items/%s/usage_records" % si_id)` at `test.py:13`.

#### New sources (APIs.guru + OpenAPI Generator)

| Repo | Source | Commit | Command | Call-site recall | Precision |
|------|--------|--------|---------|------------------|-----------|
| [dpup/git-workflow](https://github.com/dpup/git-workflow/commit/3b34e962a966550bc5e96f9b5bb7e0e58befd0d4) | APIs.guru (github fallback) | `3b34e96` parent `ba7c875` | `--dep github --from 2021-06-10 --to 2023-02-16` | 2/2 | 2/2 |
| [apache/airflow-client-python](https://github.com/apache/airflow-client-python/commit/7a2ccc6629c39fa8559cbfc0b60514655d522705) | OpenAPI Generator `spec/v2.yaml` | `7a2ccc6` parent `241405e` | `--dep airflow --from 3.0.0 --to 3.1.3` | 1/1 | 1/1 |
| **Total (new)** | | | | **3/3 (100%)** | **3/3 (100%)** |

git-workflow deletes both `requests.post('https://api.github.com/authorizations', …)` call sites in the extensionless Python script `git-github-login` (shebang-scanned) after GitHub removed the Authorizations API; the pair is resolved via APIs.guru when `github/rest-api-description` has no commit for the early from-date. airflow-client-python deletes `param_serialize(…, resource_path='/api/v2/dagReports')` in `dag_report_api.py` when regenerating after the Airflow OpenAPI drop; discovery uses the committed `spec/v2.yaml` at the client tags.

Stripe REST pair re-run: still 2/2. OpenAI large/small and non-OpenAI wheel sets were re-run after this change. Call-site recall, edit coverage, and primary counts are unchanged: large 13/13, 43/79 (54%), 43/117; small 6/6 on the call filter, 13/22 (59%), 13/20; non-OpenAI 38/43 (88%), 38/52 (73%).

## Remaining misses

- **Module globals the new wheel still assigns.** `openai.api_key`, `openai.api_type`, and `openai.api_version` are assigned in both 0.28.1 and 1.0.0, so those writes stay unflagged. `openai.api_base` is gone and is flagged.
- **A version helper.** cutler `_openai_has_legacy_audio` returns `getattr(openai, "__version__", "0")` and the caller branches on the helper. The `getattr` form is recognized inside a condition. A helper called from the condition is not.
- **Response reads on a parameter.** guardrails stream runners branch on a local `OPENAI_VERSION` string and then read `chunk["choices"]`. `chunk` is a parameter, not a return this function received from the SDK.
- **Monkeypatch strings.** dd-trace-py wraps `openai.api_requestor._make_session` and indexes resources by string. One real reference is flagged (`openai.util.convert_to_openai_object`). The string table and the `resp["choices"]` hooks are not.
- **A new file.** wandb PR 6498 adds `fine_tune.py` and deletes no SDK call.
- **Variable `getattr`, dependency injection, and positional forwarding** stay unresolved.
- **A removed parameter with no SDK call in the file.** knowledge-graph-system's `golden_describe_image.py` writes `temperature` into a comparison dict behind a `MagicMock` client. There is no `messages.create` chain, so the assignment is not flagged. The four live `ai_providers.py` writes are flagged via kwargs-origin tracing.
- **A method on a parameter.** great_expectations passes `execution_engine` and `sqlalchemy_engine` into helpers that call `.execute`. Those four deleted calls are not flagged.
- **An API that moved to another distribution.** dys-backend's pinecone v2 calls live in the `pinecone-client` wheel. The wheels named `pinecone` are stubs, and pinecone is not in the catalog, so that migration was not scored.
