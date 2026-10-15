# Outside-in mode

Outside-in starts from an **outside service** and walks inward to every call site that depends on it, including calls hidden behind your own wrappers, aliases, and re-exports. The scope is external API SDKs and REST hosts, database drivers, cloud SDKs (S3 through botocore models), and incoming webhooks such as Stripe. Ordinary libraries are out of scope. This is not Dependabot territory: no lockfile bumps, no CVE alerts, and no new-version notices. Internal repo-to-repo contracts are out of scope.

`conduit watch` and `conduit impact` already see a file that imports `openai` and calls `openai.ChatCompletion.create`. They also follow the application function that only calls your helper:

```text
app.ask  ->  llm.complete  ->  openai.ChatCompletion.create
```

`conduit map` builds that chain. The breaking line in a report is the line a wheel, model, or schema change affects. Callers of a wrapper sit in a low-confidence section unless you pass `--show-callers`. `conduit watch --packet` still treats the outer call site as a leftover of the packet's `old_callee` until that SDK call is gone. `conduit apply` is deferred.

## Command

```bash
conduit map --path ./examples/wrapped-openai-fixture --service openai
conduit map \
  --path ./examples/wrapped-openai-fixture \
  --packet ./examples/sample-packet/conduit-packet.json \
  --json
```

`--service` limits the inventory to one catalogued service (`openai`, `anthropic`, `stripe`, `twilio`, `boto3`) or any Python module name. Omit it to scan the catalog. `--packet` adds a blast radius: method renames, parameter renames and drops, and response attribute or key renames, each with a confidence. `--markdown` prints the same report with headings. `--json` is the shape Watch consumes.

The fixture under `examples/wrapped-openai-fixture` covers a function wrapper, a class method, a package re-export, `from openai import OpenAI as C`, and `requests.post` to `api.openai.com`.

## What the graph resolves

Python only, using the standard library AST (the same family of static analysis as the rest of the engines, not a learned graph).

- Imports, aliases (`OpenAI as C`), and re-exports through `__init__.py`, including an import inside a function or an enclosing function
- Calls on a name assigned from a constructor in the same function or module
- `functools.partial`, `getattr(client, "method")` when the attribute is a string literal, and a lambda assigned to a name that calls the SDK
- `self` and `Class().method` when the method is defined in the repo
- A function that `return`s a client, at medium confidence
- `requests` / `httpx` / `urllib` / `aiohttp` calls whose URL host and path can be resolved from a literal, an f-string, a constant, string concatenation, or a client `base_url`
- Branches on `package.__version__`, `importlib.metadata.version`, or `packaging.version`
- Attribute, dict, and index uses of an SDK return value, followed through assignments (`resp.choices[0].message`, `resp["data"]`)

HTTP chains show up on the map. They are not Watch leftovers: a packet rule that renames `ChatCompletion.create` cannot rewrite a raw POST body. When the OpenAI path table maps that callee to `/v1/chat/completions`, the blast radius still lists the HTTP call at medium confidence.

## How Watch uses it

For a packet whose package is a catalogued service, `scan_packet_leftovers` (Watch with `--packet`) adds a leftover at each call site whose chain ends in an `old_callee`. The import prune still finds `import openai`. The graph adds the files that only call the wrapper, and direct calls reached through a re-exported name (`from pkg import ChatCompletion`).

`conduit run` rewrites the SDK call in the wrapper, which already imports the package. It also rewrites a direct re-exported call the import prune missed, by trusting files the graph resolved. It does not rewrite `ask()` into an SDK call. After the terminal callee no longer matches `old_callee`, those wrapper leftovers clear. `conduit apply` is deferred and is not a command.

Local libraries such as pydantic are not in the service catalog. Their Watch behavior stays the import-and-call scanner.

The same graph covers database drivers (SQLAlchemy, psycopg, psycopg2, pymongo, redis, asyncpg), cloud SDKs (boto3 calls as `s3.put_object`, `google-cloud-*`, `azure-*`), and incoming Stripe, GitHub, and Twilio webhook routes. `conduit impact --dep X [--from V] [--to V]` diffs one client against another version and prints each `file:line`. The version comes from `--from`, an exact pin, `poetry.lock` / `uv.lock` / `Pipfile.lock`, the installed distribution, or the highest PyPI release matching a requirement range. The diff is the public API of the two wheels (removed, renamed, and changed-signature functions, and return types), a botocore service model, a webhook schema, and a versioned OpenAPI pair when one is recorded or can be resolved live from `--from`/`--to` against a catalogued spec history. Release notes are hints, including changelog lines that name a removed or renamed endpoint. If no version can be determined, the text is `not compared` and says why, instead of an all-clear. A pin that does not match a recorded surface says `not compared: pinned version <version> does not match a recorded surface`. REST calls with no OpenAPI pair say which calls had no spec and why. It does not bump a lockfile.

```bash
conduit impact --dep sqlalchemy --path ./examples/service-clients --fixtures ./conduit/fixtures/impact
```

## Limitations

- Python only today. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next. The JS/TS seam (`analyze_repo`) is not implemented.
- A variable passed to `getattr`, `globals()`, parameters that hold a callable, and dependency-injection containers are invisible. A string literal passed to `getattr`, `functools.partial`, and a lambda that calls the SDK are tracked. `examples/wrapped-openai-fixture/dynamic_gap.py` is the deliberate miss.
- Binding inside a function is not flow-sensitive. Positional argument forwarding is not matched to SDK parameter names. Star-imports are inventoried and not bound.
- Tests directories are skipped.
- A cloud service name that is not a string literal is not attached to the operation.
- Webhook payload fields are the ones read in the handler and a helper it calls in the same file.
- Conduit is not a Dependabot replacement. Ordinary libraries are not version-tracked.
- A re-exported call rewritten to a qualified `openai...` callee does not gain an `import openai` by itself.

A real consumer is [lm-sys/FastChat](https://github.com/lm-sys/FastChat) at the commit before PR 3032 (`98b8f642`). The six-repo backtest against merged migrations, including that commit, is in [Migration backtest](backtest.md).

## Watching the services on the map

`conduit watch` without `--packet` looks at outside services on the map. It does not clone anyone else's repo and it does not watch internal contracts.

The change list is a diff of published artifacts:

| Source | What it compares | Confidence |
|--------|------------------|------------|
| Two PyPI wheels | removed, renamed, and changed-signature functions, and return types | high for a removed call or parameter; medium for an inferred rename or a response-shape change |
| Botocore `service-2.json` | AWS operation and shape changes, by service and operation | high when the call uses the member that changed |
| Webhook schema | payload fields a handler reads | high when the field is removed; medium for a schema rename |
| Two OpenAPI documents | removed or renamed paths, method changes, and request or response fields (including newly required request fields) | high when both documents exist (fixtures or a live from/to pair from a catalogued spec history); a single latest public spec is not a diff |
| GitHub releases Atom and changelog pages | extra hints only, including an endpoint the notes say was removed or renamed | medium when a rename is only in the notes; low for changelog prose |

A newer version on PyPI or npm starts that diff. It is not a report by itself. There is no hand-written change list. `conduit/fixtures/feeds` records the public URLs (release atoms, registry JSON, changelog pages) so a demo can run offline. The wheels, botocore models, and webhook schemas live next to that directory in `conduit/fixtures/impact`. Omit `--feeds` to fetch the public URLs.

```bash
conduit watch --path ./examples/service-clients --feeds ./conduit/fixtures/feeds --json
```

`--packet` is still the leftover gate for a hop you already have. It is not a watcher for another repo's contract and it is not a Dependabot intercept.

`conduit apply` is deferred. `conduit run` still writes a frozen packet's rules into the tree.

`conduit service` is the hosted skeleton. It stores one map per customer, runs the same watch on a schedule (`tick` or `run`), flags the call sites, and writes a local markdown note. It does not rewrite the tree. There is no GitHub App.
