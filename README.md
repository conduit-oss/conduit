<p align="center">
  <img src="docs/assets/conduit-logo.svg" alt="Conduit" width="420"/>
</p>

<p align="center">
  <strong>When an outside service you depend on changes, show which lines break and help fix them.</strong>
</p>

OpenAI, Stripe, Twilio, the database you talk to, S3 through botocore, and an incoming webhook such as Stripe. Internal repo-to-repo breaks are out of scope. Pact, Optic, and Backstage own that.

Python only today. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next.

This is not Dependabot territory. Conduit does not open dependency-update PRs, bump lockfiles, scan for CVEs, or tell you that an ordinary library has a new version. It shows the lines in your code when an outside service, or the client you use to reach it, actually changes.

## The loop

Map your repo, watch the outside services on that map, and when a change lands, flag the affected lines and open a fix.

```text
map your repo → watch its outside services → change lands → flag affected lines → PR with the fix
```

| Command | What it does |
|---------|----------------|
| `conduit map` | Outside-in call graph: SDK and REST calls, database drivers, cloud operations, webhook handlers, through your wrappers, aliases, and re-exports |
| `conduit watch` | Diff old and new PyPI wheels, botocore models, and webhook schemas. Print the lines that break. Release notes are hints only |
| `conduit impact --dep X` | Same line-by-line check for one service. `--from` and `--to` pick the versions. If no version can be resolved, the note says `not compared` and why |
| `conduit service` | Hosted skeleton: keep each customer's map, run watch on a schedule, and write a local note of the lines that break |

`conduit apply` is deferred. `conduit run` still rewrites a tree from a frozen packet.

Nothing is installed in anyone else's repo.

## Install

```bash
git clone https://github.com/conduit-oss/conduit.git
cd conduit
python -m venv .venv
source .venv/bin/activate          # Windows: .venv/Scripts/activate
python -m pip install -e "./conduit[dev]"
```

## Try it

Map the wrapped OpenAI fixture (the app calls `llm.complete`, which calls the SDK):

```bash
conduit map --path ./examples/wrapped-openai-fixture
```

Watch a repo from recorded copies of the public artifacts (no network). Those files are recordings, not a hand-written change list. The change itself is the diff of the old and new wheels, botocore models, and webhook schemas under `conduit/fixtures/impact`. GitHub release notes and changelogs are extra hints.

```bash
conduit watch \
  --path ./examples/service-clients \
  --feeds ./conduit/fixtures/feeds \
  --json
```

Store the map and run one hosted pass. A hit writes a local note of the lines that break under `.conduit-service/`. Rewriting the tree is deferred (`conduit apply` is not a command):

```bash
conduit service add \
  --customer acme \
  --path ./examples/wrapped-openai-fixture \
  --store ./.conduit-service
conduit service tick \
  --store ./.conduit-service \
  --feeds ./conduit/fixtures/feeds
```

Line-by-line impact on a repo that uses SQLAlchemy, S3, a Stripe webhook, and a small recorded API client. The wheels, botocore models, and webhook schemas are files in the repo, so this does not use the network:

```bash
conduit map --path ./examples/service-clients
conduit impact --dep sqlalchemy --path ./examples/service-clients --fixtures ./conduit/fixtures/impact
conduit impact --dep boto3 --path ./examples/service-clients --fixtures ./conduit/fixtures/impact
conduit impact --dep stripe --path ./examples/service-clients --fixtures ./conduit/fixtures/impact
conduit impact --dep acme-sdk --path ./examples/service-clients --fixtures ./conduit/fixtures/impact
conduit watch --path ./examples/service-clients --feeds ./conduit/fixtures/feeds
```

`impact` prints the dependency, the version it moved from and to, the change (removed, renamed, or changed-signature function, a return type that went from a dict-like object to a typed model, a removed REST path or field, or a branch on the library version), and each `file:line` with a confidence. The SDK call inside a wrapper is the finding. Callers of that wrapper collapse to a count; `--show-callers` lists them. `watch` does the same when a newer release has a real surface to diff. A version number on its own is not a report.

The version to compare from is `--from`, else an exact `==` pin, else `poetry.lock` / `uv.lock` / `Pipfile.lock`, else the installed distribution (`importlib.metadata` or `site-packages`), else the highest PyPI release that matches a requirement range. `--to` or the latest release is the other side. The note is `not compared` only when no version can be determined, and it says why (no pin, lock, install, or resolvable range, or network disabled for a range). A pin that does not match a recorded surface says `not compared: pinned version <version> does not match a recorded surface`.

Raw `requests`, `httpx`, and `aiohttp` calls are mapped by host and path. A pair of OpenAPI documents flags removed or renamed paths and request or response fields that changed. A public spec repo (Stripe, GitHub, OpenAI) is named when only a latest document exists; that document is not a diff. Calls with no spec to compare are listed as `not compared`, with the reason.

`--packet` is still the leftover gate for a hop you already have. It fails when the pin is at the new version but old call sites remain. It does not watch internal contracts. Rewriting those sites with `conduit apply` is deferred; `conduit run` still applies a frozen packet.

## Where a change comes from

The list of changes is a diff of published artifacts. There is no hand-written change list, and the files under `conduit/fixtures/feeds` are not that list. They are offline recordings of public URLs so tests do not open a socket.

| Source | What it compares | Confidence |
|--------|------------------|------------|
| Two wheels from PyPI (or a recorded pair) | removed, renamed, and changed-signature functions, plus a return type that went from a dict-like object to a typed model | high for a removed call or parameter; medium for an inferred rename or a response-shape change |
| Botocore service model (`service-2.json`) | AWS operation and shape changes, by service and operation (`s3.PutObject`) | high when your call uses the member that changed |
| Webhook schema (Stripe OpenAPI, trimmed in tests) | payload fields a handler reads | high when the field is removed; medium when the schema says it was renamed |
| Two OpenAPI documents (or a recorded pair) | removed or renamed endpoint paths, and request or response fields that changed | high when the path or field is in both documents; a single latest public spec is not a diff |
| GitHub releases Atom and a changelog or deprecation page | extra hints only: a version, a rename the notes state, or an endpoint the notes say was removed | medium when a rename is only in the release notes; low for changelog prose |

A newer version on PyPI or npm is the trigger for that diff. It is not a notice by itself. Omit `--feeds` or `--fixtures` to fetch the public URLs and small wheels.

## Not doing

- A Dependabot or Renovate replacement: no lockfile bumps, no "upgrade this library" PRs, no CVE alerts, no new-version mail for ordinary packages
- Watching every dependency in a lockfile. Versions are read only to know which service client you have pinned
- Watching internal repos or inter-service contracts
- Installing agents in other companies' repos
- Any language other than Python on the map, watch, and impact path. TypeScript and JavaScript via tree-sitter are next, one language at a time

## What the map includes

- External API clients and REST hosts (OpenAI, Anthropic, Stripe, Twilio, and the same shape of client)
- Databases, as the driver or ORM that talks to them: SQLAlchemy, psycopg, psycopg2, pymongo, redis-py, asyncpg
- Cloud SDKs: boto3 and botocore by service and operation (`s3.put_object`), `google-cloud-*`, `azure-*`
- Incoming webhooks: FastAPI, Flask, and Django routes that receive a Stripe, GitHub, or Twilio payload, and the fields that handler reads

## Known gaps

Python only today. `functools.partial`, `getattr` with a string literal, and a lambda that wraps an SDK call are tracked. A variable passed to `getattr`, `globals()`, a callable stored on a parameter, and dependency injection stay invisible (`examples/wrapped-openai-fixture/dynamic_gap.py`). Imports inside a function, including an enclosing function, are tracked. A cloud service stored in a variable (`client(name)`) is not tied to an operation. Positional arguments are not matched to parameter names. Webhook fields read in a helper in another file are missed. REST calls with no versioned OpenAPI pair are reported as `not compared`, with the reason. The checked-in surfaces under `conduit/fixtures/impact` are recorded wheels, botocore models, and webhook schemas. Omit `--fixtures` and impact can download small wheels.

## Docs

| Topic | Link |
|-------|------|
| Outside-in map, watch, and impact | [docs/outside-in.md](docs/outside-in.md) |
| CLI reference | [docs/cli-reference.md](docs/cli-reference.md) |
| Getting started | [docs/getting-started.md](docs/getting-started.md) |
| Full index | [docs/README.md](docs/README.md) |

## License

Apache-2.0
