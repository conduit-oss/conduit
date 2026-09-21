# Residue catalog: import names and nested assign share still-old with Watch

## Problem

Conduit apply is a frozen-packet engine: schema-closed rules, a second closed set in
`SDK_RULE_TYPES` / `engine.py`, then a leftover gate and Watch that today mean **leftover call**.
`AST_IMPORT_REWRITE` rewrites module paths (plus an unguarded `str.replace`) and never walks
`from x import A, B` aliases. Nested `class Config` / class-body assigns have no rewriter; mint
prompts push them to `side_effects`. The live pydantic miss is the product of that shape: after
`@validator` becomes `@field_validator`, `from pydantic import BaseModel, validator` can stay
dirty, Watch stays clean, and smokes even assert `class Config` residue. The non-obvious constraint
is dual verdict: a name-list CST fix that Watch cannot see re-creates the lie. Constraints from
grounding: apply does not remint; `additionalProperties: false` + closed `oneOf`; unknown apply
types are silently skipped; no `package == pydantic` in core; call-rule `surface` floors stay
`AST_CALL_REWRITE`-only; uncodable multi-statement gaps stay checklist `side_effects`.

## Usage (caller's view)

Consumers already run a frozen packet with no LLM. That README loop does not change:

```bash
conduit apply --path ./examples/pydantic-validator-fixture \
  --packet ./examples/sample-packet/pydantic-validator-hop.json

conduit watch --path ./examples/pydantic-validator-fixture \
  --packet ./examples/sample-packet/pydantic-validator-hop.json --json
```

What changes is the packet they load, and what Watch's leftover list is allowed to mean.

**Call site 1 — packet author (the spec).** One name per import rule; one inner-class rule with a
key map. Not a whole-line string, not `AST_PARAM_RENAME` with `function_target: "Config"`.

```json
{
  "type": "AST_IMPORT_NAME_REWRITE",
  "target_files": ["*.py"],
  "module": "pydantic",
  "old_name": "validator",
  "new_name": "field_validator"
}
```

```json
{
  "type": "AST_NESTED_ASSIGN_REWRITE",
  "target_files": ["*.py"],
  "inner_class": "Config",
  "enclosing_base": "BaseModel",
  "assignment_target": "model_config",
  "constructor": "ConfigDict",
  "keys": { "orm_mode": "from_attributes" },
  "unmapped_keys": "refuse",
  "ensure_import": { "module": "pydantic", "name": "ConfigDict" }
}
```

`from pydantic import BaseModel, validator` is in scope without the hop fragment
`BaseModel, validator`. `from pydantic import BaseModel, BaseSettings` plus `new_module:
"pydantic_settings"` splits the statement. Methods inside `Config` are not this rule; they stay
`side_effects`.

**Call site 2 — apply leftover gate** (`evaluate_apply_leftovers`). Same function signature as
today. The return type grows a leftover *kind*. Dirty combined import or leftover inner class is
`status="dirty"` even when every `old_callee` is gone.

```python
from conduit.patcher.leftovers import evaluate_apply_leftovers

verdict = evaluate_apply_leftovers(root=tree, packet=packet)
# verdict.leftovers[i].kind in {"call", "import_name", "import_module", "nested_shape", "version_guard"}
# verdict.exit_code == 1 if any kind remains
```

**Call site 3 — Watch** (`evaluate_watch`). Pin ≥ `to_version` plus any residue kind is
`bump_dirty`. Binder completeness still runs only after leftovers are empty, and still only over
Call+decorator contracts.

```python
from conduit.watch import evaluate_watch

watch = evaluate_watch(root=tree, packet=packet)
payload = watch.to_dict()
# payload["leftovers"] stays human display lines (CI can still grep "validator")
# payload["leftover_kinds"] names which still-old families fired
```

Mint (`normalize_llm_rule`) is a fourth, producer-side call: statement-shaped
`AST_IMPORT_REWRITE` becomes `AST_IMPORT_NAME_REWRITE` before freeze; PascalCase bare
`function_target` param-renames are dropped. Apply never sees those wire mistakes.

## Shape

Data first: `InPlaceImportName | SplitImportName`, `NestedAssignOp`, and a `ResidueCatalog` of
still-old specs compiled from the same packet. `packet/rewrite_ops.py` is the boundary. Engine
elif branches call `parse_*` then CST apply; leftovers/Watch call `residues_from_packet` then
`scan_residue_catalog`. Wire `dict` keys do not leak into Watch.

Import completeness is an `ImportFrom` name walk (preserve `asname`; star-import refuse + leftover).
Split-import is a distinct type, not a boolean on the in-place op: drop the alias, merge into
`from {to_module} import …`, remove an emptied statement. Nested config is one SDK rule that
replaces a nested `ClassDef` of simple assigns with `target = Constructor(remapped_kwargs)` and
optional `ensure_import`. `unmapped_keys=refuse` plus any non-assign body skips that node so
half-migration stays visible as `nested_shape` residue.

Module-path `AST_IMPORT_REWRITE` stays; its string fallback is restricted to dotted-module tokens
with an `old not in new` guard. Name rewrite has no string path.

Interface depth: authors and CLI keep two new closed rule types plus the existing apply/watch
entry points. Split-merge, star fail-close, ClassDef surgery, key remaps, and catalog compile sit
behind those. Binder `UseKind` does not grow — mixing Config into completeness would make "proof
eligible surface" mean two things. `side_effects` remains the checklist for uncodable gaps.

Invariants in types: split vs in-place; `UnmappedKeys` enum; identifier patterns on schema
`old_name`/`new_name` so comma-fragments cannot validate as name rewrites. Validate at parse;
transformers trust ops. Idempotent: already-new names and already-replaced classes are 0 CST hits;
leftover scan is the second apply's truth. Single compile of residue specs so apply and Watch
cannot disagree on what still-old is.

Python-only for the new ops; other suffixes skip (reported), so Watch cannot call a skipped JS
file clean for a rule that never ran.

## Synthesis decision

(orchestrator fills)

## Tradeoffs accepted

- We accept two new closed rule types (schema + `SDK_RULE_TYPES` + engine branches + mint
  allowlists) in exchange for not overloading `old_import` strings or `AST_PARAM_RENAME`.
- We accept a breaking leftover field rename (`callee` → `token` + `kind`) in exchange for a
  dual-verdict that can name import and nested residue without pretending they are calls.
  Display strings stay greppable.
- We accept Watch remaining blind to Config when the packet leaves it in `side_effects`, in
  exchange for leftover meaning "packet-declared mechanical residue" rather than a pydantic linter.
- We accept Python-only name/nested transforms in exchange for not inventing JS/Java/Go class-body
  semantics in this slice.
- We accept split-import requiring a companion `DEPENDENCY_ADD` in the packet, in exchange for
  apply never inventing dependencies.
- We accept mint promotion of single-name `from M import A` statements only, in exchange for not
  guessing which names in a multi-name LLM blob were intended.
- We accept hop-smoke packet rewrite (fragment `old_import` dies) in exchange for killing the
  unguarded substring fallback that made `validator` ⊂ `field_validator` unsafe.

## Alternatives considered

- **Extend `AST_IMPORT_REWRITE` with optional `names` on the same opaque strings.** Lost: mint
  already emits statements, fragments, and module paths into one field; optional names leave that
  ambiguity on the public wire. Callers would still learn three encodings. Interface looks smaller
  and is shallower.
- **Compose Config from `KEY_RENAME` + `AST_PARAM_RENAME`.** Lost: `KEY_RENAME` is REST-quoted;
  `function_target` matches Call chains. Authors would coordinate two wrong families plus
  `side_effects` for the class wrapper. Complexity sits on the packet author, not the patcher.
- **Keep leftovers call-only; only fix CST apply.** Lost: the current dual-verdict bug. Apply
  could succeed on names while Watch greened on calls. Fails rubric item 5 by construction.
- **Grow binder `UseKind` with import and nested-class observations.** Lost: completeness is
  proof over declared CALL surfaces. Nested `Config` is not an export-path use; stuffing it into
  `contracts_from_packet` leaks structural residue into a Call-shaped binder and forces
  proof-eligibility policy onto ClassDef.
- **Refuse split-import (drop name or skip the rule).** Lost: `BaseSettings` → `pydantic_settings`
  is a real packet already in live mint. Refuse pushes a mechanical split back to `side_effects`
  and re-teaches authors the hop-fragment workaround.
- **Independent post-apply grep oracle for `old_name` / `class Config`.** Lost: substring
  `validator` hits `field_validator`; second matcher is information leakage. Residue scan over AST
  is the same knowledge apply used, not a parallel guess.

## Open questions and risks

- Should `ensure_import` live on `AST_NESTED_ASSIGN_REWRITE` (one author-facing rule) or stay a
  sibling `AST_IMPORT_NAME_REWRITE` so nested-assign never touches imports?
- Relative imports (`from .foo import Bar`): exact `module` string match only, or resolve against
  package layout? The sketch does exact match.
- After split, is merge-into-existing-`ImportFrom` allowed to reorder aliases, and do we preserve
  trailing commas / parens multiline imports?
- Version-guard leftovers stay untyped relative to the packet; should they remain in the same
  `Leftover` tuple now that kinds exist, or become a separate Watch arm?
- Do published packets that used statement-shaped `AST_IMPORT_REWRITE` get a one-time normalize
  on load, or only mint-time promotion (hand-authored hop JSON must be edited)?
- Nested assign matching any enclosing class when `enclosing_base` is omitted: too broad for
  codebases with unrelated inner `Config`?

## Next implementation step

Add the two schema `$defs` (both copies), `rewrite_ops.py` parse/catalog types, and a libcst
`rewrite_python_import_names` test against `from pydantic import BaseModel, validator` before
touching Watch display — residue compile can hang off the same parsed ops on the next commit.
