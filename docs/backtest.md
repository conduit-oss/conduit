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

## Known-break evaluation

This set is separate from the fractions above. Six public repositories were chosen, and the expected `file:line` pairs were written to [`docs/known-breaks.json`](known-breaks.json), before any `conduit watch` or `conduit impact` on those commits. That file was not edited after the runs. Each repository pins an old client and calls an API that the upstream changelog says was removed. Four of the six are non-OpenAI. The services are SQLAlchemy, PyMongo, and stripe-python. [`scripts/known_break_eval.py`](../scripts/known_break_eval.py) clones the recorded commit, runs live `conduit impact --dep <service> --json`, and scores it. A catch is a non-wrapper impact site at that file and line. A false positive is any other non-wrapper site. Sites reached only through a wrapper are left out of the false-positive count. Deprecations are printed in their own section and are neither breaks nor false positives. The machine score is [`docs/known-breaks-results.json`](known-breaks-results.json).

The pin `sqlalchemy>=1.4.2,<2` resolves to 1.4.54. These live comparisons use SQLAlchemy 2.1.5. The recorded fixtures still compare 1.4.52 with 2.1.4.

| Repo | Service | Versions | Expected | Caught | Missed | False positives | Notes |
|------|---------|----------|----------|--------|--------|-----------------|-------|
| [PaddlePaddle/PaddleNLP](https://github.com/PaddlePaddle/PaddleNLP/commit/3f87dac9b719f75399f92f8bf634ae2ef0611832) `slm/pipelines` | sqlalchemy `>=1.4.2,<2` | 1.4.54 → 2.1.5 | 1 — `pipelines/document_stores/sql.py:755` `Query.from_self` ([migration](https://docs.sqlalchemy.org/en/20/changelog/migration_20.html#selecting-from-the-query-itself-as-a-subquery-e-g-from-self)) | 1 | 0 | 0 | `session.query(...).from_self` binds because the installed package's return type for `query` is `Query`. |
| [diffgram/diffgram](https://github.com/diffgram/diffgram/commit/bfd8690827f3c46ddc566d2e878183dccf300948) | sqlalchemy `==1.3.23` | 1.3.23 → 2.1.5 | 1 — `shared/database/task/task.py:247` `Query.select_entity_from` ([migration](https://docs.sqlalchemy.org/en/20/changelog/migration_20.html#selecting-entities-from-alternative-selectables-query-select-entity-from)) | 1 | 0 | 0 | The untyped `session` parameter is the object yielded by `sessionmaker`. The caller reaches it through a project star import whose module lives under a parent package. |
| [LAION-AI/Open-Assistant](https://github.com/LAION-AI/Open-Assistant/commit/f1e6ed9526f5817531f3ab85441a40b3671ddccb) `backend` | sqlalchemy `==1.4.41` | 1.4.41 → 2.1.5 | 2 — `oasst_backend/tree_manager.py:1075`, `oasst_backend/prompt_repository.py:985` `Query.select_entity_from` (same migration note) | 2 | 0 | 0 | `self.db` is a `Session`. The scored line is the method name, which is the line after the receiver. |
| [Sniper970119/dianping_spider](https://github.com/Sniper970119/dianping_spider/commit/5d4e0018c2ceb02064f11329bd70b5b49b53259c) | pymongo `==3.11.3` | 3.11.3 → 4.19.0 | 5 — `utils/saver/mongo_saver.py:66,77,88` `Collection.insert` ([insert](https://pymongo.readthedocs.io/en/4.0/migrate-to-pymongo4.html#collection-insert-is-removed)); `utils/database_utils.py:57,76` `Collection.update` ([update](https://pymongo.readthedocs.io/en/4.0/migrate-to-pymongo4.html#collection-update-is-removed)) | 5 | 0 | 0 | |
| [TencentBlueKing/bk-chatbot](https://github.com/TencentBlueKing/bk-chatbot/commit/4e7dc7a97606f548a831a3809470e682f508ae60) | pymongo `==3.11.0` | 3.11.0 → 4.19.0 | 1 — `common/mongodb/client.py:69` `Collection.update` (same update note) | 1 | 0 | 0 | One wrapper caller of `db.update` is excluded from the false-positive count. |
| [chdsbd/kodiak](https://github.com/chdsbd/kodiak/commit/2c23f609c9dc37ef679be7734bf2aa9541d45f0c) `web_api` | stripe `==2.46.0` | 2.46.0 → 16.0.0 | 1 — `web_api/models.py:1135` `Invoice.upcoming` ([stripe-python 12.0.0](https://github.com/stripe/stripe-python/blob/master/CHANGELOG.md#12-0-0)) | 1 | 0 | 1 | `web_api/test_views.py:549` is another call of the removed `Invoice.upcoming` and was not in the pre-registered list. The same file, line, and change is emitted once. `SignatureVerificationError` is still exported on 16.0.0. The class symbol is not flagged. |

Across the six repositories: 11 expected sites, 11 caught, 0 missed, 1 false-positive site. Recall 11/11. Precision 11/12.

A later rerun, after wrapper demotion and subscript tracking, scored the same six commits again. The table did not move. A rerun after flow-sensitive instance fields scored the same six commits again. That table did not move. This rerun, after inherited and re-exported members stay on the new wheel, kept the same eleven catches. Kodiak's extra site is `test_views.py:549`, emitted once.

## Fresh repositories

These three were chosen after the return-type, attribute, deprecation, and re-export changes. None is an OpenAI client. The expected `file:line` pairs were written to [`docs/known-breaks-fresh.json`](known-breaks-fresh.json) before `conduit impact` on these commits, and that file was not edited after the run. The machine score is [`docs/known-breaks-fresh-results.json`](known-breaks-fresh-results.json).

Django is not an outside service on this rerun. It only reaches a database through a driver it imports. WebODM pins Django 2.2.27 and does not call that driver, so the driver is not on the map either. The 141 expected lines stay in the ground-truth file and are not scored as misses.

| Repo | Service | Versions | Expected | Caught | Missed | False positives | Notes |
|------|---------|----------|----------|--------|--------|-----------------|-------|
| [WebODM/WebODM](https://github.com/WebODM/WebODM/commit/51c8f98b35de920d347631bcb99ed06180a6cafd) | django `==2.2.27` | not an outside service | 141 — `url`, `ugettext`, `is_safe_url`, `JSONField`, `NullBooleanField` ([removed in 4.0](https://docs.djangoproject.com/en/4.0/releases/4.0/#features-removed-in-4.0)) | 0 | 0 | 0 | `impact --dep django` exits because the pinned release is not an outside service. The map has no Django entry. WebODM never calls the database driver, so that driver is not added either. |
| [phasehq/console](https://github.com/phasehq/console/commit/c99ea21252ac6587b45460a2ae6d8a3c1a9761ad) `backend` | stripe `==10.5.0` | 10.5.0 → 16.0.0 | 1 — `ee/billing/graphene/queries/stripe.py:145` `Invoice.upcoming` ([stripe-python 12.0.0](https://github.com/stripe/stripe-python/blob/master/CHANGELOG.md#12-0-0)) | 1 | 0 | 0 | |
| [samuelclay/NewsBlur](https://github.com/samuelclay/NewsBlur/commit/dae86cde1a584c19a2642d74f3118f38fcf41fef) | pymongo `>=3,<4` | 3.13.0 → 4.19.0 | 7 — `Collection.count` / `insert` in `utils/backups/copy_mongo_serialized.py`, `Collection.update` at `utils/archive/bootstrap_story_hash.py:27`, `Collection.remove` at `utils/archive/bootstrap_mongo.py:261`, `NotMasterError` at `flask_monitor/db_monitor.py:170` and `:200` | 3 | 4 | 2 | `settings.MONGODB` now follows the project settings module assignment through `mongoengine.connect` to `MongoClient`, so `bootstrap_story_hash.py:27` is caught. The four remaining misses use `pymongo.Connection` (not on the pinned wheel) or a `mongoengine.connect` return that is not the settings handle. |

phasehq and NewsBlur are the two repos whose dependency is still an outside service. Across those two: 8 expected sites, 4 caught, 4 missed, 2 false-positive sites. Recall 4/8. Precision 4/6. A rerun after config-module attribute tracking and `mongoengine.connect` return typing moved NewsBlur from 2/7 to 3/7.

## Third set

These three were chosen after wrapper demotion and subscript tracking. Each scored dependency is an outside service, the three services are different, and none is OpenAI. The expected lines were written to [`docs/known-breaks-third.json`](known-breaks-third.json) from the library tags and the call sites, before `conduit impact` on these commits. That file was not edited after the run. The machine score is [`docs/known-breaks-third-results.json`](known-breaks-third-results.json).

| Repo | Service | Versions | Expected | Caught | Missed | False positives | Notes |
|------|---------|----------|----------|--------|--------|-----------------|-------|
| [ckan/ckan](https://github.com/ckan/ckan/commit/f2d47c8f87bc5f224ebff93b23d0f6fda8fb81b4) | sqlalchemy `==1.3.5` | not compared | 1 — `ckan/model/activity.py:121` `Query.select_entity_from` | 0 | 1 | 0 | PyPI publishes 1.3.5 as an sdist only. Impact compares wheels, so the note is `not compared: could not fetch sqlalchemy wheel(s) 1.3.5`. The call was not scored. |
| [d-Rickyy-b/pastepwn](https://github.com/d-Rickyy-b/pastepwn/commit/0239d71b005c87cf0b982db5a20c1173c1362587) | pymongo `>=3.11.3` | 3.11.3 → 4.19.0 | 2 — `pastepwn/database/mongodb.py:40` `Cursor.count`, `:43` `Collection.count` | 2 | 0 | 0 | `self.db` is assigned from the client and then from `self.db[dbname]`. The read of `self.collection` uses that later subscript. Both `count` calls are caught. |
| [Dataherald/dataherald](https://github.com/Dataherald/dataherald/commit/f8946182e6db55522b88cd5a831c788339b8fc42) `services/enterprise` | stripe `==8.0.0` | 8.0.0 → 16.0.0 | 2 — `utils/billing.py:16` `Invoice.upcoming`, `scripts/record_usage.py:91` `SubscriptionItem.create_usage_record` ([stripe-python 12.0.0](https://github.com/stripe/stripe-python/blob/master/CHANGELOG.md#12-0-0)) | 2 | 0 | 0 | Both removed methods were caught. A webhook schema for this client was not fetched; that note is not a call-site miss. |

The first pass on Dataherald said stripe was not an outside service. The package names `api.stripe.com`, and the evidence string stayed empty because no transport call was attributed. A declared host is now enough evidence. That repo was scored again after that change. CKAN is still not compared, because 1.3.5 has no wheel. pastepwn was 0/2 before instance fields followed the assignment that reaches the read. This rerun is 2/2, with no extra sites. Dataherald stayed 2/2.

Across the two repositories whose dependency was compared: 4 expected sites, 4 caught, 0 missed, 0 false-positive sites. Recall 4/4. Precision 4/4. CKAN's expected line stays in the ground-truth file and is not a compared miss. A rerun after inherited and re-exported members stay on the new wheel scored the same three commits again. That table did not move.

## Fourth set

These three were chosen after instance fields became flow-sensitive. The expected lines were written to [`docs/known-breaks-fourth.json`](known-breaks-fourth.json) from the call sites and the upstream removal notes, before `conduit impact` on these commits. That file was not edited after the run. The machine score is [`docs/known-breaks-fourth-results.json`](known-breaks-fourth-results.json). The scored services are PyMongo and stripe-python. None is OpenAI. ARTIF reaches PyMongo through subscript (`client[database].collection.method`).

| Repo | Service | Versions | Expected | Caught | Missed | False positives | Notes |
|------|---------|----------|----------|--------|--------|-----------------|-------|
| [CRED-CLUB/ARTIF](https://github.com/CRED-CLUB/ARTIF/commit/cc744deafe0781b9c5730339ead299c967d88dfd) | pymongo `==3.11.0` | 3.11.0 → 4.19.0 | 4 — `pre_scorer.py:695` and `:712` `Collection.insert`, `mongo_update.py:299` and `cron.py:42` `Collection.remove` ([insert](https://pymongo.readthedocs.io/en/4.0/migrate-to-pymongo4.html#collection-insert-is-removed), [remove](https://pymongo.readthedocs.io/en/4.0/migrate-to-pymongo4.html#collection-remove-is-removed)) | 4 | 0 | 0 | All four pre-written calls were caught. The previous run's nine `Cursor.skip` and `Cursor.limit` sites are gone: those methods are defined on the private cursor base and re-exported with the public `Cursor`. They were not added to the ground-truth file. |
| [justdjango/facial-recognition-saas](https://github.com/justdjango/facial-recognition-saas/commit/8af024e02ade8d84997537a6220f327021f050a2) | stripe `==2.28.1` | 2.28.1 → 16.0.0 | 2 — `core/views.py:118` `Invoice.upcoming`, `:229` `UsageRecord.create` ([stripe-python 12.0.0](https://github.com/stripe/stripe-python/blob/master/CHANGELOG.md#12-0-0)) | 2 | 0 | 0 | |
| [makerspace/makeradmin](https://github.com/makerspace/makeradmin/commit/be643c45781bc75219bc88f0f9d6e338d7a1d95a) `api` | stripe `==7.8.2` | 7.8.2 → 16.0.0 | 2 — `src/shop/stripe_subscriptions.py:484` and `:486` `Invoice.upcoming` (same stripe-python note) | 2 | 0 | 0 | A webhook schema for this client was not fetched; that note is not a call-site miss. |

Across the three repositories: 8 expected sites, 8 caught, 0 missed, 0 false-positive sites. Recall 8/8. Precision 8/8. The previous run was 8/17 because of the nine cursor sites. The matcher was not changed after this score.

## Fifth set

These three were chosen after a class keeps members it inherits, re-exports, or receives dynamically. The expected lines were written to [`docs/known-breaks-fifth.json`](known-breaks-fifth.json) from the call sites and the upstream removal notes, before `conduit impact` on these commits. That file was not edited after the run. The machine score is [`docs/known-breaks-fifth-results.json`](known-breaks-fifth-results.json). Each scored dependency is an outside service. The services are PyMongo, stripe-python, and SQLAlchemy. None is OpenAI. Manga-Tagger reaches PyMongo through subscript (`client[database][collection].method`).

| Repo | Service | Versions | Expected | Caught | Missed | False positives | Notes |
|------|---------|----------|----------|--------|--------|-----------------|-------|
| [Inpacchi/Manga-Tagger](https://github.com/Inpacchi/Manga-Tagger/commit/d459df30dab06afc28b9c32d521aaa97dea388e9) | pymongo `==3.11.0` | 3.11.0 → 4.19.0 | 2 — `MangaTaggerLib/database.py:194` `Collection.insert`, `:210` `Collection.update` ([insert](https://pymongo.readthedocs.io/en/4.0/migrate-to-pymongo4.html#collection-insert-is-removed), [update](https://pymongo.readthedocs.io/en/4.0/migrate-to-pymongo4.html#collection-update-is-removed)) | 2 | 0 | 0 | `cls._database = super()._database[...]` now keeps the subscript result, so the calls are `Collection.insert` and `Collection.update`. |
| [wevote/WeVoteServer](https://github.com/wevote/WeVoteServer/commit/751923f02015694251ae98f2ab0e778f0f5f465e) | stripe `==8.7.0` | 8.7.0 → 16.0.0 | 2 — `donate/controllers.py:107` and `stripe_donations/controllers.py:111` `Invoice.upcoming` ([stripe-python 12.0.0](https://github.com/stripe/stripe-python/blob/master/CHANGELOG.md#12-0-0)) | 2 | 0 | 0 | Both pre-written calls were caught. |
| [indico/indico](https://github.com/indico/indico/commit/ddcf9153dcf2ffe04916ff8e2006098d32ad606d) | sqlalchemy `==1.4.54` | 1.4.54 → 2.1.5 | 4 — `users/util.py:257` and `persons/controllers.py:516` `Query.from_self`, `queries.py:34` and `editable_list.py:225` `Query.select_entity_from` ([from_self](https://docs.sqlalchemy.org/en/20/changelog/migration_20.html#selecting-from-the-query-itself-as-a-subquery-e-g-from-self), [select_entity_from](https://docs.sqlalchemy.org/en/20/changelog/migration_20.html#selecting-entities-from-alternative-selectables-query-select-entity-from)) | 3 | 1 | 21 | Both `from_self` sites and `queries.py:34` `model.query.select_entity_from` are caught via Query return types and class-parameter inference. `editable_list.py:225` is still a miss: the chain is `session.query(...).select_entity_from` and does not bind as `Query.select_entity_from` for impact. Duplicate file:line/change rows are collapsed. The ground-truth file was not edited. |

Across the three repositories: 8 expected sites, 7 caught, 1 missed, 21 false-positive sites. Recall 7/8. Precision 7/28.

Indico's 21 extras, checked against the [2.0 migration guide](https://docs.sqlalchemy.org/en/20/changelog/migration_20.html) and the 2.1.5 wheel, and not added to the ground truth: `DDL.execute` and `CreateSchema.execute` (12 sites) are real removals of connectionless `Executable.execute`. `orm.mapper` (8 sites) is a false positive / ground-truth gap for impact: every site is `@listens_for(orm.mapper, ...)`, and 2.1.5 still exports that symbol for event listeners (calling `mapper()` as a constructor fails, but these sites only register listeners). `Mapper.iterate_properties` (1 site) is a false positive: the method is still defined in 2.1.5.

## Type checker comparison

Pyright and Jedi were tried as receiver resolvers on the 18 known-break repos. The head-to-head is [`docs/type-checker-compare.json`](type-checker-compare.json). Pyright did not raise recall and hurt kodiak precision; Jedi typed Manga-Tagger's `_database` as `NoneType`. The hand-written resolver stayed. The optional Pyright wiring was removed after that experiment.
