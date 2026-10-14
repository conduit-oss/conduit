<p align="center">
  <img src="docs/assets/conduit-logo.svg" alt="Conduit" width="420"/>
</p>

<p align="center">
  <strong>When an outside service you depend on changes, show which lines break and help fix them.</strong>
</p>

OpenAI, Stripe, Twilio, the database you talk to, S3, and an incoming webhook. Internal repo-to-repo breaks are out of scope. Pact, Optic, and Backstage own that. Python only today.

This is not a Dependabot replacement. Conduit does not open dependency-update PRs, bump lockfiles, scan for CVEs, or tell you that an ordinary library has a new version. It shows the lines in your code when an outside service, or the client you use to reach it, actually changes.

## The loop

Map your repo, watch the outside services on that map, and when a change lands, flag the affected lines and open a fix.

```text
map your repo → watch its outside services → change lands → flag affected lines → PR with the fix
```

| Command | What it does |
|---------|----------------|
| `conduit map` | Every place your code reaches an outside service: SDK calls, database drivers, cloud operations, webhook handlers, your own wrappers |
| `conduit watch` | Read feeds those services already publish. When a release changes a call you make, print the lines |
| `conduit impact` | Same line-by-line check for one service, on demand |
| `conduit apply` | Apply a migration packet to your tree |
| `conduit service` | Keep each customer's map, run watch on a schedule, flag call sites, and produce the fix PR |

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

Watch that service from recorded feeds (no network). The packet names the version and, when the feed says so, the old name and the new name:

```bash
conduit watch \
  --path ./examples/wrapped-openai-fixture \
  --feeds ./conduit/fixtures/feeds \
  --json
```

Store the map and run one hosted pass. This writes a local fix PR (dry-run `conduit apply`, no GitHub App) under `.conduit-service/`:

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

`impact` prints the dependency, the version it moved from and to, the change, and each `file:line` in your code with a confidence. `watch` does that when the feed shows a newer release and a recorded surface is available. A version number on its own is not a report.

`--packet` is still the leftover gate for a hop you already have. It fails when the pin is at the new version but old call sites remain. That check belongs to apply. It does not watch internal contracts.

```bash
conduit apply \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json \
  --dry-run
```

## What watch reads

| Feed | What it catches | Confidence |
|------|-----------------|------------|
| Two wheels from PyPI (or a recorded pair) | removed or renamed calls, parameters, and deprecations on the client you import | high for a removed call or parameter; medium when a rename is inferred from the signature |
| Botocore service model (`service-2.json`) | AWS operation and shape changes, by service and operation (`s3.PutObject`) | high when your call uses the member that changed |
| Webhook schema (Stripe OpenAPI, trimmed in tests) | payload fields a handler reads | high when the field is removed; medium when the schema says it was renamed |
| GitHub releases Atom, `github.com/<org>/<repo>/releases.atom` | SDK release, and a rename when the notes state one | high for the version; medium when a rename is only in the notes |
| PyPI and npm | that a service client you already call published a newer version | high, and only as the trigger for the diff above |
| Public OpenAPI, previous file vs latest | path and parameter changes | medium |
| Changelog or deprecation page | prose renames and deprecations | low |

Sources are pluggable. Tests use the recorded fixtures under `conduit/fixtures/feeds` and do not open a socket. Omit `--feeds` to fetch the public URLs.

## Not doing

- A Dependabot or Renovate replacement: no lockfile bumps, no "upgrade this library" PRs, no CVE alerts, no new-version mail for ordinary packages
- Watching every dependency in a lockfile. Versions are read only to know which service client you have pinned
- Watching internal repos or inter-service contracts
- Installing agents in other companies' repos
- More languages before one paying team uses Python

## What the map includes

- External API clients and REST hosts (OpenAI, Anthropic, Stripe, Twilio, and the same shape of client)
- Databases, as the driver or ORM that talks to them: SQLAlchemy, psycopg, psycopg2, pymongo, redis-py, asyncpg
- Cloud SDKs: boto3 and botocore by service and operation (`s3.put_object`), `google-cloud-*`, `azure-*`
- Incoming webhooks: FastAPI, Flask, and Django routes that receive a Stripe, GitHub, or Twilio payload, and the fields that handler reads

## Known gaps

Python only. Misses functions looked up by name at runtime (`getattr`) and functions passed in as arguments. A cloud service stored in a variable (`client(name)`) is not tied to an operation. Positional arguments are not matched to parameter names. Webhook fields read in a helper in another file are missed. Tests live under `conduit/fixtures/impact` and do not download wheels.

## Docs

| Topic | Link |
|-------|------|
| Outside-in map and feeds | [docs/outside-in.md](docs/outside-in.md) |
| CLI reference | [docs/cli-reference.md](docs/cli-reference.md) |
| Getting started | [docs/getting-started.md](docs/getting-started.md) |
| Full index | [docs/README.md](docs/README.md) |

## License

Apache-2.0
