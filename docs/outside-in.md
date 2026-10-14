# Outside-in mode

Outside-in starts from an **outside service** and walks inward to every call site that depends on it, including calls hidden behind your own wrappers, aliases, and re-exports. The scope is external API SDKs and REST hosts, database drivers, cloud SDKs (S3 through botocore models), and incoming webhooks such as Stripe. Ordinary libraries are out of scope. This is not Dependabot territory: no lockfile bumps, no CVE alerts, and no new-version notices. Internal repo-to-repo contracts are out of scope.

Watch and apply already rewrite a file that imports `openai` and calls `openai.ChatCompletion.create`. They miss the application function that only calls your helper:

```text
app.ask  ->  llm.complete  ->  openai.ChatCompletion.create
```

`conduit map` builds that chain. Watch and apply then treat the outer call site as a leftover of the packet's `old_callee` until the SDK call at the end of the chain is gone.

## Command

```bash
conduit map --path ./examples/wrapped-openai-fixture --service openai
conduit map \
  --path ./examples/wrapped-openai-fixture \
  --packet ./examples/sample-packet/conduit-packet.json \
  --json
```

`--service` limits the inventory to one catalogued service (`openai`, `anthropic`, `stripe`, `twilio`, `boto3`) or any Python module name. Omit it to scan the catalog. `--packet` adds a blast radius: method renames, parameter renames and drops, and response attribute or key renames, each with a confidence. `--markdown` prints the same report with headings. `--json` is the shape Watch and apply consume.

The fixture under `examples/wrapped-openai-fixture` covers a function wrapper, a class method, a package re-export, `from openai import OpenAI as C`, and `requests.post` to `api.openai.com`.

## What the graph resolves

Python only, using the standard library AST (the same family of static analysis as the rest of the engines, not a learned graph).

- Imports, aliases (`OpenAI as C`), and re-exports through `__init__.py`
- Calls on a name assigned from a constructor in the same function or module
- `self` and `Class().method` when the method is defined in the repo
- A function that `return`s a client, at medium confidence
- `requests` / `httpx` / `urllib` calls whose URL host is a known vendor

HTTP chains show up on the map. They are not Watch leftovers: a packet rule that renames `ChatCompletion.create` cannot rewrite a raw POST body. When the OpenAI path table maps that callee to `/v1/chat/completions`, the blast radius still lists the HTTP call at medium confidence.

## How Watch and apply use it

For a packet whose package is a catalogued service, `scan_packet_leftovers` (Watch and `conduit apply`) adds a leftover at each call site whose chain ends in an `old_callee`. The import prune still finds `import openai`. The graph adds the files that only call the wrapper, and direct calls reached through a re-exported name (`from pkg import ChatCompletion`).

Apply rewrites the SDK call in the wrapper, which already imports the package. It also rewrites a direct re-exported call the import prune missed, by trusting files the graph resolved. It does not rewrite `ask()` into an SDK call. After the terminal callee no longer matches `old_callee`, those wrapper leftovers clear.

Local libraries such as pydantic are not in the service catalog. Their Watch behavior stays the import-and-call scanner.

The same graph covers database drivers (SQLAlchemy, psycopg, psycopg2, pymongo, redis, asyncpg), cloud SDKs (boto3 calls as `s3.put_object`, `google-cloud-*`, `azure-*`), and incoming Stripe, GitHub, and Twilio webhook routes. `conduit impact --dep X [--from V] [--to V]` diffs the pinned client (or `--from`) against `--to` or the newer recorded surface and prints each `file:line`. The diff is the public API of the two wheels (removed, renamed, and changed-signature functions, and return types), a botocore service model, and a webhook schema. Release notes are hints. If the repo does not pin the client and `--from` is omitted, the text is `not compared: no pinned version found` instead of an all-clear. A pin that does not match a recorded surface says `not compared: pinned version <version> does not match a recorded surface`. It does not bump a lockfile.

```bash
conduit impact --dep sqlalchemy --path ./examples/service-clients --fixtures ./conduit/fixtures/impact
```

## Limitations

- Python only today. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next. The JS/TS seam (`analyze_repo`) is not implemented.
- `getattr`, `globals()`, parameters that hold a callable, and dependency-injection containers are invisible. `examples/wrapped-openai-fixture/dynamic_gap.py` is the deliberate miss.
- Binding inside a function is not flow-sensitive. Positional argument forwarding is not matched to SDK parameter names. Star-imports are inventoried and not bound.
- Tests directories are skipped.
- A cloud service name that is not a string literal is not attached to the operation.
- Webhook payload fields are the ones read in the handler and a helper it calls in the same file.
- Conduit is not a Dependabot replacement. Ordinary libraries are not version-tracked.
- A re-exported call rewritten to a qualified `openai...` callee does not gain an `import openai` by itself.

A real consumer to try next is [lm-sys/FastChat](https://github.com/lm-sys/FastChat) at the commit before PR 3032 (`98b8f642`), which wraps the OpenAI 0.28 client the same way this fixture does.

## Watching the services on the map

`conduit watch` without `--packet` looks at outside services on the map. It does not clone anyone else's repo and it does not watch internal contracts.

The change list is a diff of published artifacts:

| Source | What it compares | Confidence |
|--------|------------------|------------|
| Two PyPI wheels | removed, renamed, and changed-signature functions, and return types | high for a removed call or parameter; medium for an inferred rename or a response-shape change |
| Botocore `service-2.json` | AWS operation and shape changes, by service and operation | high when the call uses the member that changed |
| Webhook schema | payload fields a handler reads | high when the field is removed; medium for a schema rename |
| GitHub releases Atom and changelog pages | extra hints only | medium when a rename is only in the notes; low for changelog prose |

A newer version on PyPI or npm starts that diff. It is not a report by itself. There is no hand-written change list. `conduit/fixtures/feeds` records the public URLs (release atoms, registry JSON, changelog pages) so a demo can run offline. The wheels, botocore models, and webhook schemas live next to that directory in `conduit/fixtures/impact`. Omit `--feeds` to fetch the public URLs.

```bash
conduit watch --path ./examples/service-clients --feeds ./conduit/fixtures/feeds --json
```

`--packet` is still the leftover gate for a hop you already have. That path is part of apply. It is not a watcher for another repo's contract and it is not a Dependabot intercept.

`conduit apply` writes the frozen packet's rules into the tree. `--dry-run` prints the edits and does not write files. It does not open a pull request.

`conduit service` is the hosted skeleton. It stores one map per customer, runs the same watch on a schedule (`tick` or `run`), flags the call sites, and writes a local fix PR by calling `conduit apply` (dry-run unless you pass `--apply`). There is no GitHub App.
