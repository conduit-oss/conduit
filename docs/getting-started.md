# Getting started

Start with `conduit map`, `conduit watch`, `conduit impact`, and `conduit apply`. `conduit service` is the hosted skeleton for that loop.

Scope is outside services only: external API SDKs and REST, databases, cloud SDKs such as S3 via botocore models, and incoming webhooks such as Stripe. This is not Dependabot territory. Conduit does not bump lockfiles, scan for CVEs, or send new-version notices for ordinary libraries. It does not watch internal repos.

Watch without `--packet` diffs old and new PyPI wheels, botocore models, and webhook schemas. GitHub releases and changelogs are extra hints. There is no hand-written change list. `conduit impact --dep X [--from V] [--to V]` is the same check for one service and prints `file:line`. A repo with no pin says `not compared: no pinned version found` instead of an all-clear. The `--packet` form of Watch is the leftover gate for a hop you already have. Apply and that gate use no LLM. Producers can still author packets. Detect modules are Advanced.

Python only today. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next.

## Requirements

- Python 3.10+
- Git
- Optional: [`gh`](https://cli.github.com/) (only if you want Conduit to open PRs)
- Optional: an LLM (only for packet synthesis, self-correct, and smoke-test generation)

## Install

From a clone of this repo (always use a virtualenv so the `conduit` CLI lands on your PATH):

```bash
git clone https://github.com/conduit-oss/conduit.git
cd conduit
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e "./conduit[llm,langs,dev]"
conduit --help
```

The public repo is [conduit-oss/conduit](https://github.com/conduit-oss/conduit).

Extras:

| Extra | Installs |
|-------|----------|
| `llm` | OpenAI Python SDK (also used to talk to Ollama / custom servers) |
| `llm-anthropic` | Anthropic SDK |
| `langs` | tree-sitter grammars used by packet-apply engines. The outside-service path does not use them yet |
| `llm-js` | Alias subset: tree-sitter + JS/TS grammars |
| `llm-all` | LLM SDKs + language grammars |
| `dev` | pytest |

## Languages

`conduit map`, `conduit watch`, and `conduit impact` read Python only. Other languages are planned one at a time. TypeScript and JavaScript via tree-sitter are next.

`conduit apply` still runs frozen packet rules through the language engines already in the tree (Python via libcst; JS/TS, Java, and Go via tree-sitter when `[langs]` is installed, with a regex fallback). Those engines are not the outside-service product. String and regex rules also apply to YAML, JSON, and `.env*` files.

## 1. Apply a Migration Packet (recommended first run)

The repo ships a tiny consumer stuck on **openai 0.28** (`openai.ChatCompletion.create`) plus a sample Migration Packet that performs the structural 0.28 → 1.x hop. That sample is the offline harness proof for apply and Watch. It does not need LLM keys.

```bash
conduit run \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json \
  --demo \
  --skip-tests \
  --skip-pr
```

What you should see:

1. A packet is loaded (`openai-0.28.1-1.0.0`)
2. Files importing `openai` are pruned (typically `src/ai_client.py`)
3. Codemods rewrite `ChatCompletion.create` → modern chat completions and bump the pin. Then openai-only `apply_openai_client_chain` polish runs (`OpenAI()` + `client.*`). Packet rules alone are not the full openai kill bar.
4. PR creation is skipped so you can inspect the working tree

`--demo` forces offline fixtures (and the openai demo packet fallback) and skips the consumer `OPENAI_API_KEY` gate used for live OpenAI verify. Use `--skip-tests` for the structural kill-bar proof (demo tests assert the legacy surface on purpose; run the suite after you accept the migrated tree, or restore with checkout).

```bash
git -C examples/demo-consumer diff
# or restore:
git -C examples/demo-consumer checkout -- .
```

`--skip-pr` does **not** mean dry-run. Files are actually modified. It only skips `git push` / `gh pr create`.

Preview transforms without writing:

```bash
conduit apply \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json \
  --dry-run
```

## 2. Watch outside services, or gate a hop you already have

`conduit watch` without `--packet` diffs the outside services on the map. Pass `--feeds ./conduit/fixtures/feeds` for the recorded public URLs. The wheels and schemas beside that directory are the change data.

```bash
conduit watch --path ./examples/service-clients --feeds ./conduit/fixtures/feeds
conduit impact --dep sqlalchemy --path ./examples/service-clients --fixtures ./conduit/fixtures/impact
```

`conduit watch --packet` is a separate read-only gate. It fails when the pin is at the packet `to_version` but call sites still use packet `old_callee` symbols. It does not rewrite files. It does not intercept Dependabot, bump a lockfile, or watch another repo.

```bash
conduit watch \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json
```

Exit `0` while the pin is still at `from_version` (warns if leftovers exist). Exit non-zero when the pin has reached `to_version` and leftovers remain. Exit `0` when the pin is at `to_version` and leftovers are empty.

Wire the same gate in CI: [GitHub Actions](github-actions.md). CLI details: [CLI reference](cli-reference.md#conduit-watch).

**Merge bar:** Watch/apply leftover-clean means the mechanical packet work is done. Packet `side_effects` and any remaining leftovers print as a **Human checks** block (also **Double-check** in the PR body) — copy that list into the PR when humans still need to redesign or accept permanent gaps (`uncodable` deletes, `multi_step` kwargs). Leftover-clean is not full unbounded v2 completeness.

## 3. Produce a packet (maintainers)

Vendors and maintainers author a hop from migrate-guide URLs, dry-run it, then publish into a catalog. No LLM keys are required. Without enrichment, `packet new` writes a schema-valid dependency hop and recorded `sources`. Add `--scaffold-only` or omit a configured model for the keyless path.

1. **Author** from links (`from_version` is `*` at author time; `to_version` is the target you pick):

```bash
conduit packet new \
  --package google-genai \
  --ecosystem pypi \
  --version 1.0.0 \
  --source-url https://googleapis.github.io/python-genai/
```

2. **Test** (validate; optional dry-run apply with `--path`):

```bash
conduit packet test --packet ./packets/google-genai-pypi-1.0.0.json
```

3. **Publish** into a catalog checkout or git URL:

```bash
conduit packet publish \
  --packet ./packets/google-genai-pypi-1.0.0.json \
  --catalog /path/to/conduit-packets
```

Full authoring: [Migration packets](migration-packets.md). Flags: [CLI reference](cli-reference.md#conduit-packet).

## Run on your own repository

```bash
conduit run --path /path/to/your/repo --packet openai -v
# or an explicit packet file
conduit run --path /path/to/your/repo --packet ./packets/openai-1.0.0.json
# apply + test locally, no PR
conduit run --path /path/to/your/repo --packet openai --skip-pr
```

On Windows, prefer forward slashes or quoted paths if backslashes get stripped by the shell:

```bash
conduit run --path "C:/Users/you/my-repo" --packet openai -v
```

Full flags: [CLI reference](cli-reference.md#conduit-run). If Conduit must guess placeholder versions (`0.0.0` / `1.0.0`) or fall back to the openai demo fixture, it prints a **warning**. Prefer a real manifest pin and a published packet so apply and Watch stay grounded.

## Configure an LLM (optional)

Apply and Watch do **not** need an LLM. Configure one if you want packet enrichment at author time, self-correct after verify, or auto smoke tests.

```bash
# Local Ollama. No API key.
export CONDUIT_LLM_PROVIDER=ollama
export CONDUIT_LLM_MODEL=llama3.2
```

Details: [LLM configuration](llm.md).

## Advanced: detect modules (private-factory adjacent)

`conduit detect` and `conduit module new` author vendor signals and scaffold modules. They are not step 1 for consumers. Read the leave-public-OSS note in [Detect modules](detect-modules.md) before you invest in that path.

## Next steps

- Author or share rules: [Migration packets](migration-packets.md)
- Wire Watch in CI: [GitHub Actions](github-actions.md)
- Pipeline overview: [Architecture](architecture.md)
