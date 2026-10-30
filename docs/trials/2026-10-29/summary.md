# Conduit real-repo trials, 2026-10-29

These are results from running Conduit on 8 open-source repos at Conduit `main` @ `41d7fcb`. All times are ET.

**No forks were created.** The cursor-github connector has no fork tool. A check of `xlor1009` on 2026-10-29 found none of the 8 repos, so each one is a read-only clone of upstream.

All edits stayed on the trial box. Nothing was pushed to any upstream or fork, and no issues, PRs or comments were opened upstream.

## Setup

**Model settings.** Model calls use the Conduit defaults (Nebius Token Factory):

- Nemotron-3 Ultra for rewrites and Nano for checks
- thinking off
- the default per-site token cap

**Workflow, per repo.**

1. Create a Python venv with uv and install the old SDK pin plus `-e conduit`.
2. Run `conduit map`.
3. Run `conduit impact --dep D --from OLD --to NEW --json`.
4. Run `conduit baseline --test-cmd ...`.
5. Install the NEW pin.
6. Run `conduit apply --impact ... --write --test-cmd ... --baseline ... --json`. Each apply was time-boxed at 15 to 25 minutes.
7. Re-run the tests after apply (`*-tests-after.txt`).

`watch` was run on llmflows only. It takes more than 5 minutes per repo.

## Results

| repo | migration | map sites (all / dep) | impact changes / sites | baseline tests (failing) | apply outcome | tests after apply | tokens |
|---|---|---|---|---|---|---|---|
| stoyan-stoyanov/llmflows | openai 0.28.1→1.109.1 | 111 / 103 | 12 / 118 | 47 (1) | 19 needs-human, 99 skipped, 0 fixed | collection error (8 errors): `openai.error` import | 118,002 (71 calls) |
| stoyan-stoyanov/llmflows | anthropic 0.3.11→0.34.2 | — / 0 | **impact refused**: "anthropic is not an outside service" (exit 2) | — | not run | — | 0 |
| Forethought-Technologies/AutoChain | openai 0.27.8→1.109.1 | 84 / 17 | 8 / 8 | 17 (1) | 3 needs-human, 5 skipped, 0 fixed; empty diff | 3 failed / 14 passed (2 new failures) | 22,676 (15 calls) |
| perone/feste | cohere 4.57→5.0.0 | 8 / 0 (cohere not mapped) | **impact refused**: "cohere is not an outside service" (exit 2) | 21 (0) | not run | — | 0 |
| mysociety/mapit.mysociety.org | stripe 9.9.0→16.1.0 | 322 / 207 | 1 (`Invoice.upcoming` removed) / 7 | **not runnable**: needs PostGIS/GEOS, and apt could not find the packages | 1 fixed (unverified: no tests), 6 skipped | not run | 2,073 (2 calls) |
| microsoft/gpt-review | openai 0.28.1→1.109.1 | 100 / 34 | 4 / 56 | 64 (13) | 3 "fixed, not covered by tests", 1 "fixed, needs review", 52 skipped | collection error: tests import `openai.openai_object` | 17,935 (9 calls) |
| zilliztech/GPTCache | openai 0.28.1→1.109.1 | 611 / 37 | 3 / 6 | 25 (16) | **timed out at 15 min**, no JSON, no edits | collection error | not reported (5 calls made) |
| Subscribie/subscribie | stripe 9.8.0→16.1.0 | 3249 / 266 | 1 (`Invoice.upcoming` removed) / 13 | 27 (0) | **timed out at 15 min**, no JSON, no edits | 26 passed, 1 skipped (`Invoice.upcoming` has no test coverage) | not reported (4 calls made) |
| CuriousLearner/django-phone-verify | twilio 7.16.0→9.12.0 | 5 / 5 | 0 / 0 | 144 (0) | nothing to apply | 144 passed on twilio 9.12.0 (true negative) | 0 |

The "map sites" column counts site lines in the service section of `conduit map`, before the Reachability section. "dep" counts only the sites for the migrated SDK.

## Per-repo notes

### llmflows

**openai:** 0 of 118 sites fixed.

- The package does `from openai.error import ...` at import time in `llmflows/llms/openai.py:9`, and every module imports through `llmflows/llms/__init__.py`.
- After the bump, every test file fails at collection. Conduit verifies one site at a time, so no single rewrite can make the tests pass. Every rewrite was reverted ("Reverted: the tests failed with this rewrite"), and the import cascade left the rest as needs-human.
- The tests also patch `"openai.ChatCompletion.create"` by string, so they would fail on 1.x even if every library site were fixed.

**False positives in impact.** Callers of the shared `call_with_retry` wrapper in `claude_chat.py` and `palm*.py` were attributed to openai `Completion.create`. `examples/*` were included.

**anthropic:** blocked by the outside-service gate. The 0.3.11 wheel is classified transport (`anthropic -> anyio -> socket`), and `map` leaves anthropic out of `services`.

### AutoChain

- **Why the diff is empty but 2 tests now fail:** Conduit made no edits. All 3 attempted rewrites (`chat_openai.py:189`, `ada_embedding.py:35`, `base.py:114`) failed the tests and were reverted. The other 5 sites were skipped as "same function".
- `test_chat_completion` and `test_ada_encoder` fail only because the venv now has openai 1.109.1 while the code and test mocks are unchanged. The tests `mock.patch("openai.ChatCompletion.create")` and `"openai.Embedding.create"`, which do not exist in 1.x.
- So even a correct rewrite fails these tests. Conduit does not migrate test mocks.

### feste

- `impact` refused: "cohere is not an outside service" (exit 2). The cohere 4.57 wheel ships an aiohttp backend (`cohere -> aiohttp -> ssl`), so the gate classifies it as transport.
- `map` also misses cohere completely, even though `feste/backend/cohere.py` calls `cohere.Client(...)` and `.generate`. Only openai appears in the map.

### mapit.mysociety.org

- The tests need Postgres/PostGIS and GEOS. `apt-get` on the trial box could not find `libgeos-dev`, `postgis` or `postgresql`, so setup stopped there.
- map, impact and apply ran without a test command.
- The one rewrite is **semantically wrong**. It replaced `stripe.Invoice.upcoming(customer=...)` with `stripe.Invoice.list(customer=..., status='draft', limit=1)`. The correct stripe ≥ v12 replacement is `stripe.Invoice.create_preview(customer=...)`; a draft invoice is not the upcoming-invoice preview.
- The rewrite is labelled "fixed (unverified: no tests)", so it would need review.

### gpt-review

- The status "not covered by tests" is misleading. After the bump the whole suite fails at collection, because `tests/test_openai.py` imports `openai.error` and conftest imports `openai.openai_object` and patches `"openai.ChatCompletion.create"`. Conduit read that as "no passing test" rather than "tests broken by the bump".
- **Azure regression:** the Azure branch in `_openai.py` was rewritten to `openai.OpenAI().chat.completions.create(model=...)`, replacing `deployment_id=` and dropping Azure routing. It should use `AzureOpenAI`. Because it was labelled "not covered by tests", the regression went unflagged.
- `_llama_index.py:141`: the rewrite deleted `api_base`/`api_type` from `model_kwargs` instead of mapping them.
- `context.py:59` was flagged "needs review" for a dropped dependency, which is correct.
- 23 of the 52 skipped entries duplicate an identical (path, line, expr) "wrapper caller" entry.

### GPTCache

- **impact miss:** only `Embedding.create` and `api_base` were found. The real code subclasses removed classes (`class ChatCompletion(openai.ChatCompletion, BaseCacheLLM)` in `gptcache/adapter/openai.py:31`, plus Completion, Audio, Image and Moderation in the same file). Removed-symbol checks miss base-class use.
- `map` took about 11 minutes, and `impact` took more than 6 minutes of CPU.
- `apply` spent 2 to 7 minutes of CPU between model calls and hit the 15-minute timeout after 5 calls.
- **A timed-out apply leaves no partial JSON or token total.**
- The first apply attempt exited 2 in under a second because of a bug in our runner: a relative impact path was resolved after `cd` into the repo. That was not a Conduit problem; it was re-run with absolute paths.

### subscribie

- impact found `Invoice.upcoming` removed at 13 sites.
- apply was CPU-bound between model calls (about 4.5 minutes per site) and timed out at 15 minutes. It fetched PyPI JSON for stripe repeatedly (37 GETs), with no caching.
- The tests pass on stripe 16 without changes, because `Invoice.upcoming` is not covered by them.
- `map` lists bogus services `{}.api.mailchimp.com` (an unformatted f-string) and `invoice.created` (a webhook event name).

### django-phone-verify

- The twilio range was chosen as 7.16.0 → 9.12.0 (latest).
- The code uses `from twilio.rest import Client as TwilioRestClient`, `messages.create` and `TwilioRestException`, all stable across that range.
- impact reports 0 changes. The 144 tests pass on 9.12.0, so this is a correct true negative.

## Conduit bugs and gaps for the backlog (not filed)

1. **The outside-service gate rejects real API SDKs.** cohere 4.57 (aiohttp backend) and anthropic 0.3.11 (anyio) are classified `transport=True`, so `impact` exits 2 and `map` leaves them out. This blocked feste completely and blocked llmflows/anthropic.
2. **Per-site verification cannot succeed when the old import breaks collection.** Examples are a module-level `from openai.error import ...` (llmflows) and tests that patch removed symbols by string or import them (AutoChain, gpt-review). Conduit needs batch or whole-file verification, and it needs to migrate test files and mocks or at least report "tests incompatible with new SDK".
3. **"No passing test" is reported as "fixed, not covered by tests"** when the suite actually fails at collection after the bump (gpt-review). This should be its own status.
4. **Semantic rewrites go wrong when tests can't catch them:**
   - Azure `ChatCompletion` was rewritten to a plain `OpenAI()` client (gpt-review).
   - `Invoice.upcoming` was rewritten to `Invoice.list(status='draft')` instead of `create_preview` (mapit).
   - Known migration mappings should be supplied to the model.
5. **Wrapper-caller attribution crosses SDKs.** Callers of a shared retry wrapper were attributed to openai in the claude/palm modules (llmflows).
6. **Duplicate result entries.** The same (path, line, expr) "wrapper caller" appears more than once (23 duplicates in gpt-review).
7. **Removed-class detection misses subclassing** (`class X(openai.ChatCompletion)` in GPTCache).
8. **apply performance on mid and large repos.** It spends minutes of CPU between model calls (it looks like a whole-repo re-analysis per site) and makes repeated PyPI fetches without a cache. GPTCache and subscribie hit 15 minutes after 4 or 5 sites. map and impact are also slow on GPTCache.
9. **A timed-out or killed apply writes no partial JSON or token total.** Results should be streamed or checkpointed.
10. **map service noise:** an unformatted f-string host (`{}.api.mailchimp.com`) and a webhook event name (`invoice.created`) are listed as services.
11. **`watch`/default target picks the latest major** (openai 3.41.0) rather than the next major. The trials pinned 1.109.1 by hand.
12. **`impact` includes `examples/`** by default. That is arguably noise for library repos.
