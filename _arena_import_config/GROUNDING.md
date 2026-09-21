# Grounding: import-list rewrite + nested config transform

**Question:** How Conduit apply/mint/Watch handle AST import + config-shaped migrations, and where generic multi-name import rename + nested class/config rewrite must plug in (packet-driven; no `package == pydantic` in core).

**Explorers:** [Explore packet schema mint](1e420897-fd1c-4a0a-86f8-748f356b1aa7), [Explore AST import rewrite](b2935116-233b-4b05-a74b-719fdbd226a4), [Explore apply rewrite families](140934df-0419-4ef6-93b2-30362f832096), [Explore Watch leftovers surfaces](739142af-86a5-4eaa-b5b2-7efd9a3560ef).

## Constraints (must honor)

- Frozen packet is the apply contract; apply does not remint / invent rules.
- Schema `additionalProperties: false` + closed `rule.oneOf`; dual schema copies (`schema/` + `conduit/schema/`).
- Apply registry is a second closed set (`SDK_RULE_TYPES` / `REST_RULE_TYPES` + `engine.py` elif) or new types are silently skipped.
- No package-specific branches in core; pydantic only in examples/smokes.
- Uncodable multi-statement gaps stay in `side_effects` (checklist, not executed).
- Call-rule `surface` floors are `AST_CALL_REWRITE`-only; binder/completeness are Call+decorator uses only.

## Mental model

```
mint (LLM) → normalize/validate → freeze packet JSON
apply: partition SDK→REST → per-file transformers → surface CALL pass → leftover gate
Watch: same leftover scan (CALL old_callee only) + binder completeness
```

## Import rewrite today

- **Schema:** `AST_IMPORT_REWRITE` = opaque `old_import` / `new_import` strings. Prompt says “module path.” Live mint emits full statements; hop smoke emits clause fragment `BaseModel, validator`.
- **AST (`ast_import_rewrite.py`):** `leave_Import` rewrites module aliases; `leave_ImportFrom` rewrites **module path only** — never walks `names`. Comment at L48–49 claiming name rewrite is false.
- **Fallback:** if AST changes==0 and `old_import in content`, raw `str.replace` with **no** `old in new` guard (CALL mop-up has that guard).
- **Miss:** `from pydantic import BaseModel, validator` + rule `from pydantic import validator` → substring miss → dirty import after CALL rewrote `@validator`.
- **Hop workaround:** fragment `old_import` so string fallback hits; second apply stays idempotent. Not a general engine.

## Apply family map (relevant)

| Type | Stage | What it touches | Config / multi-import? |
|------|-------|-----------------|------------------------|
| `AST_IMPORT_REWRITE` | sdk | module paths (+ string fallback) | No name-level |
| `AST_CALL_REWRITE` | sdk | Call chains + mop-up | Decorator Call yes; import no |
| `AST_ATTR_RENAME` | sdk | Attribute chains | Not `orm_mode = True` Assign |
| `AST_PARAM_RENAME` | sdk | Call kwargs | Schema-legal `function_target=Config` is wrong for class body |
| `KEY_RENAME` | rest | quoted JSON/YAML/env | Not unquoted class assigns |
| `SURFACE_DEFINITE_REWRITE` | synthetic | Call only | No Import/ClassDef |
| `side_effects` | n/a | PR checklist | Config prose today |

**No** `leave_ClassDef` / class-body Assign rewriter in patcher. Nested `class Config` not indexed (`_collect_bases` = module-level only).

## Watch / leftovers

- Leftovers = `ast.Call` matching `AST_CALL_REWRITE.old_callee` only. **Not** `old_import`, unused import names, or `class Config`.
- Completeness observes Call+decorator uses on proof-eligible surfaces; unused import aliases are indexed but never observed → status can be `complete` with dirty combined import.
- CI Watch demo gates on leftover exit code / CALL presence, not import-clause or Config.
- Existing smokes **assert** `class Config` residue remains.

## Plug-in points (facts, not design)

1. **Name-level import:** `_ImportRewriteTransformer.leave_ImportFrom` walk `ImportAlias`; schema/normalize may need module+names or overloaded semantics; kill/limit unsafe string fallback for short identifiers; optional leftover arm for ImportFrom names.
2. **Nested config:** new SDK rule type (+ schema + stage + engine elif) with libcst ClassDef/Assign surgery; packet carries class name + key map + target assignment form; residual uncodable keys stay `side_effects`. Do not alias onto `AST_PARAM_RENAME`.
3. **Mint:** prompts currently push Config → `side_effects`; reverse for mechanical subset; extend `_LLM_RULE_ALLOWED_KEYS` / prompts / dedupe / `scope.py` tokens.
4. **Idempotency:** name rewrite must be AST (not bare substring) because `validator` ⊂ `field_validator`.

## Open questions for arena

1. Extend `AST_IMPORT_REWRITE` vs new sibling type for name-level / split-module.
2. Config: one “inner class → assignment” rule + key map vs many key rules + residual `side_effects`.
3. Should leftover/Watch grow import-name (and/or Config) oracle, or keep calls-only contract and accept dirty-import-blind Watch?
4. Split-import when one name moves modules (`BaseSettings` → `pydantic_settings`): split statement, drop name, or refuse?
5. Forbid mint `AST_PARAM_RENAME` with nested-class `function_target` as fake Config migration?
