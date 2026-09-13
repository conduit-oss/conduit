# Codemods

The patcher applies packet `rules` to the pruned file set. Implementation: [`conduit/src/conduit/patcher/`](../conduit/src/conduit/patcher/).

## Common fields

Most file-targeting rules include:

```json
"target_files": ["*.py", "src/**/*.ts"]
```

Globs match basename or repo-relative path (`fnmatch`).

## Rule catalog

### `EXACT_STRING_REPLACE`

```json
{
  "type": "EXACT_STRING_REPLACE",
  "target_files": ["*.py", "*.ts", "*.js", "*.yaml", "*.yml", "*.json", ".env*"],
  "match": "gpt-4-0613",
  "replace": "gpt-4o"
}
```

Matches whole tokens only (boundaries treat `A–Z a–z 0–9 _ . -` as part of a token). That prevents `davinci` from rewriting `text_davinci_003` in a `def` name, and `gpt-4` from rewriting inside `gpt-4-0613` — which would otherwise inject `-` / `.` into identifiers and cause `SyntaxError`.

### `REGEX_REPLACE`

```json
{
  "type": "REGEX_REPLACE",
  "target_files": ["*.py"],
  "pattern": "OpenAI\\(\\s*\\)",
  "replace": "OpenAI()"
}
```

### `AST_PARAM_RENAME`

Renames a keyword / named parameter near a matching call:

- **Python** — libcst kwargs
- **JS/TS** — object property keys in call args (tree-sitter when `langs` installed)
- **Java** — builder-style `.oldParam(...)` method names
- **Go** — struct-literal keys (`OldParam:`)

```json
{
  "type": "AST_PARAM_RENAME",
  "target_files": ["*.py", "*.ts", "*.js", "*.java", "*.go"],
  "function_target": "chat.completions.create",
  "old_param": "max_tokens",
  "new_param": "max_completion_tokens"
}
```

`function_target` may be a dotted path; matching is suffix-aware (calls ending in `.create` can match).

### `AST_PARAM_DROP`

Omits a keyword / named parameter near a matching call (Python libcst; JS/TS heuristic). Optional `values` limits drops to those literals (e.g. only `temperature=0`).

```json
{
  "type": "AST_PARAM_DROP",
  "target_files": ["*.py", "*.ts", "*.js"],
  "function_target": "chat.completions.create",
  "param": "temperature",
  "values": [0, 0.0]
}
```

### `AST_IMPORT_REWRITE`

Rewrites import / module paths via the language engine for the file suffix (libcst for Python; tree-sitter import literals for JS/TS, Java, and Go).

```json
{
  "type": "AST_IMPORT_REWRITE",
  "target_files": ["*.py", "*.ts", "*.js", "*.java", "*.go"],
  "old_import": "openai",
  "new_import": "openai"
}
```

### `AST_ATTR_RENAME`

Renames attribute / member-access chains (e.g. `openai.ChatCompletion` → `openai.chat.completions`).

```json
{
  "type": "AST_ATTR_RENAME",
  "target_files": ["*.py", "*.ts", "*.js", "*.java", "*.go"],
  "old_attr": "openai.ChatCompletion",
  "new_attr": "openai.chat.completions"
}
```

### `AST_CALL_REWRITE`

Rewrites call / callee paths the same way as attribute rename, targeting call expressions.

```json
{
  "type": "AST_CALL_REWRITE",
  "target_files": ["*.py", "*.ts", "*.js", "*.java", "*.go"],
  "old_callee": "openai.ChatCompletion.create",
  "new_callee": "openai.chat.completions.create"
}
```

### `KEY_RENAME`

Rewrites **quoted** request/response/config keys (`data["max_tokens"]`, JSON fixtures, quoted YAML) and `.env*` line prefixes. Unlike `AST_PARAM_RENAME`, this is not limited to call kwargs.

Config candidates (`.env*`, `*.yaml` / `*.yml` / `*.json` / `*.toml` / `*.ini`) are always unioned into the apply set, even when import-prune dropped them. Source files still come from the existing allowlist. Other string rules are **not** broadened to every config file.

```json
{
  "type": "KEY_RENAME",
  "old_key": "max_tokens",
  "new_key": "max_completion_tokens",
  "target_files": ["*.py", "*.ts", "*.js", "*.json", "*.yaml", "*.yml", "*.toml", ".env*"]
}
```

### `DEPENDENCY_BUMP`

Updates manifests (`requirements.txt`, `pyproject.toml`, `package.json`, `go.mod`, `pom.xml`, `build.gradle` / `.kts`). Always considered even if the file was not import-pruned. `pyproject.toml` is edited with **tomlkit** (PEP 621 arrays and Poetry maps), not regex.

```json
{
  "type": "DEPENDENCY_BUMP",
  "package": "openai",
  "from_version": "0.28.1",
  "to_version": "1.0.0",
  "ecosystems": ["pip", "pyproject", "npm", "go", "maven", "gradle"]
}
```

### `DEPENDENCY_ADD` / `DEPENDENCY_REMOVE`

Companion packages for splits (e.g. `langchain` → `langchain-core`). Packet `to_version` is a **bare** version (`1.2.3`); apply formats the pin per manifest (`pkg==1.2.3`, npm/Poetry `^1.2.3`, Go `v1.2.3`). Optional `scope`: `main` (default), `dev`, or npm `peer`.

ADD/REMOVE default to `pip` + `pyproject` unless `ecosystems` is set. They do not create missing Poetry groups, PEP 621 extras, or `requirements-dev.txt`. Maven/Gradle ADD/REMOVE are skipped. Lockfiles are not regenerated — Double-check asks you to run `poetry lock` / `npm install` / `go mod tidy`.

```json
{
  "type": "DEPENDENCY_ADD",
  "package": "langchain-core",
  "to_version": "0.2.0",
  "scope": "main",
  "ecosystems": ["pip", "pyproject"]
}
```

## Language engines

Pluggable engines under `conduit.patcher.languages` dispatch AST rules by file suffix.
Missing optional grammars fall back to regex/string transforms (apply never hard-fails).
Optional formatters (`gofmt`, `prettier`, `google-java-format`) run after edits when present on `PATH`.

| Language | Engine |
|----------|--------|
| Python | libcst (`PythonEngine`) |
| JS/TS | tree-sitter when `langs` / `llm-js` extra installed; regex/string fallbacks |
| Java | tree-sitter (`tree-sitter-java`) + fallbacks |
| Go | tree-sitter (`tree-sitter-go`) + `gofmt` when available |
| YAML/JSON/env | String / regex / `KEY_RENAME` |

Install grammars:

```bash
pip install -e "./conduit[langs]"
# or with LLM extras:
pip install -e "./conduit[llm,langs,dev]"
```


## Apply CLI

```bash
conduit apply --path . --packet ./conduit-packet.json
conduit apply --path . --packet ./conduit-packet.json --dry-run
```

`conduit run` calls the same engine after prune + export delta.

## Safety rails

- Hard directory exclusions (see [Pruning](pruning-and-export-delta.md))
- Optional vendor-context check (`file_has_vendor_context`) so rules don’t fire in unrelated files
- Dry-run mode prints planned edits without writing

## Related docs

- [Migration packets](migration-packets.md)
- [Pruning & export delta](pruning-and-export-delta.md)
