# Getting started

Consumers load a packet, apply it, then gate leftovers with Watch. Producers author and publish packets. Detect modules are Advanced. Skip them until you need vendor signals.

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

The `conduit-oss/conduit` repo is **private**. Cloning requires GitHub access to that org.

Extras:

| Extra | Installs |
|-------|----------|
| `llm` | OpenAI Python SDK (also used to talk to Ollama / custom servers) |
| `llm-anthropic` | Anthropic SDK |
| `langs` | tree-sitter + JS/TS, Java, and Go grammars for richer AST transforms |
| `llm-js` | Alias subset: tree-sitter + JS/TS grammars |
| `llm-all` | LLM SDKs + all language grammars |
| `dev` | pytest |

## Supported languages

| Target language | AST engine | Notes |
|-----------------|------------|-------|
| Python | libcst | Default; demo consumer is Python |
| JavaScript / TypeScript | tree-sitter (`langs` extra) | Regex fallback without grammars |
| Java | tree-sitter (`langs` extra) | Import + call/attr + builder params |
| Go | tree-sitter (`langs` extra) | Import paths + selectors; `gofmt` if available |

String/regex rules also apply to YAML, JSON, and `.env*` files. Install `[langs]` for richer non-Python AST transforms before any LLM step.

## 1. Apply a Migration Packet (recommended first run)

The repo ships a tiny consumer stuck on **openai 0.28** (`openai.ChatCompletion.create`) plus a sample Migration Packet that performs the structural 0.28 → 1.x hop.

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
3. Codemods rewrite `ChatCompletion.create` → modern chat completions, bump the pin, then client-chain polish (`OpenAI()` + `client.*`)
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

## 2. Watch the pin (CI gate)

After a Dependabot bump, fail CI when the pin is at the packet `to_version` but call sites still use packet `old_callee` symbols. Read-only; does not rewrite files.

```bash
conduit watch \
  --path ./examples/demo-consumer \
  --packet ./examples/sample-packet/conduit-packet.json
```

Exit `0` while the pin is still at `from_version` (warns if leftovers exist). Exit non-zero when the pin has reached `to_version` and leftovers remain. Exit `0` when the pin is at `to_version` and leftovers are empty.

Wire the same gate in CI: [GitHub Actions](github-actions.md). CLI details: [CLI reference](cli-reference.md#conduit-watch).

## 3. Produce a packet (maintainers)

Vendors and maintainers author a hop from migrate-guide URLs, dry-run it, then publish into a catalog.

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

Primary edits do **not** need an LLM. Configure one if you want synthesis / self-correct / auto smoke tests.

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
