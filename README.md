<p align="center">
  <img src="docs/assets/conduit-logo.svg" alt="Conduit" width="420"/>
</p>

<p align="center">
  <strong>When an outside service you depend on changes, show which lines break and help fix them.</strong>
</p>

OpenAI, Stripe, Twilio, and the rest. Internal repo-to-repo breaks are out of scope. Pact, Optic, and Backstage own that. Python only today.

## The loop

Map your repo, watch the outside services on that map, and when a change lands, flag the affected lines and open a fix.

```text
map your repo → watch its outside services → change lands → flag affected lines → PR with the fix
```

| Command | What it does |
|---------|----------------|
| `conduit map` | Every place your code reaches an outside service: direct SDK calls, your own wrappers, renamed imports, raw HTTP |
| `conduit watch` | Read feeds those services already publish and turn a change into a packet |
| `conduit apply` | Apply that packet to your tree |
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
| GitHub releases Atom, `github.com/<org>/<repo>/releases.atom` | SDK release | high for the version; medium when a rename is only in the notes |
| PyPI and npm | a package version newer than your pin | high |
| Public OpenAPI, previous file vs latest | path and parameter changes | medium |
| Changelog or deprecation page | prose renames | low |

Sources are pluggable. Tests use the recorded fixtures under `conduit/fixtures/feeds` and do not open a socket. Omit `--feeds` to fetch the public URLs.

## Not doing

- Watching internal repos or inter-service contracts
- Installing agents in other companies' repos
- More languages before one paying team uses Python

## Known gaps

Python only. Misses functions looked up by name at runtime (`getattr`) and functions passed in as arguments. Packets from feeds are thin: a version, and an old name to a new name when the feed actually says so.

## Docs

| Topic | Link |
|-------|------|
| Outside-in map and feeds | [docs/outside-in.md](docs/outside-in.md) |
| CLI reference | [docs/cli-reference.md](docs/cli-reference.md) |
| Getting started | [docs/getting-started.md](docs/getting-started.md) |
| Full index | [docs/README.md](docs/README.md) |

## License

Apache-2.0
