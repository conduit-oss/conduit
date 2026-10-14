# Outside-in mode

Outside-in starts from an **external service** (a vendor SDK or a REST host) and walks inward to every call site that depends on it, including calls hidden behind your own wrappers.

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

## Limitations

- JS/TS is a seam (`analyze_repo`) and is not implemented.
- `getattr`, `globals()`, parameters that hold a callable, and dependency-injection containers are invisible. `examples/wrapped-openai-fixture/dynamic_gap.py` is the deliberate miss.
- Binding inside a function is not flow-sensitive. Positional argument forwarding is not matched to SDK parameter names. Star-imports are inventoried and not bound.
- Tests directories are skipped.
- A re-exported call rewritten to a qualified `openai...` callee does not gain an `import openai` by itself.

A real consumer to try next is [lm-sys/FastChat](https://github.com/lm-sys/FastChat) at the commit before PR 3032 (`98b8f642`), which wraps the OpenAI 0.28 client the same way this fixture does.

## Watching the services on the map

`conduit watch` without `--packet` polls feeds those services already publish. It does not clone anyone else's repo and it does not watch internal contracts.

| Source | Feed | Confidence |
|--------|------|------------|
| GitHub releases | `https://github.com/<org>/<repo>/releases.atom` | high for the version, medium when a rename is only in the notes |
| PyPI | `https://pypi.org/pypi/<package>/json` | high |
| npm | `https://registry.npmjs.org/<package>` | high, and only when the repo pins that package |
| OpenAPI | previous spec vs latest spec | medium |
| Changelog or deprecation page | the service's public page | low |

A change becomes a migration packet: package, old version, new version, and old name to new name when the feed says so. Pass `--feeds` with recorded fixtures to run offline. Omit it to fetch the public URLs.

```bash
conduit watch --path ./examples/wrapped-openai-fixture --feeds ./conduit/fixtures/feeds --json
```

`--packet` is still the leftover gate for a hop you already have. That path is part of apply. It is not a watcher for another repo's contract.

`conduit service` stores one map per customer, runs the same watch on a schedule (`tick` or `run`), flags the call sites the packet would break, and writes a local fix PR by calling `conduit apply` (dry-run unless you pass `--apply`).
