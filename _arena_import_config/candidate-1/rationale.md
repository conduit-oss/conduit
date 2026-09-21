# Candidate 1 — rationale

## Problem

Conduit's frozen packet can rename call chains and module paths, but two
common migration moves are invisible to the engine: renaming a symbol
inside `from x import A, B` (and aliases), and rewriting a nested
config-shaped class into an assignment plus key renames. Today
`AST_IMPORT_REWRITE` only rewrites ImportFrom **module** nodes; when that
misses, an unguarded `str.replace` is the only fallback — which is why the
pydantic hop smoke ships a clause fragment (`BaseModel, validator`) instead
of honest semantics, and why `validator` ⊂ `field_validator` makes bare
substring rewrite unsafe. Nested `class Config` has no rewriter at all;
mint prompts push it to `side_effects`, and Watch leftovers are
**call-only**, so apply can leave dirty imports / Config residue while
Watch stays green. Constraints from grounding: frozen packet is the apply
contract; schema + `SDK_RULE_TYPES` are dual closed sets; no
`package == pydantic` in core; do not overload `AST_PARAM_RENAME` /
`KEY_RENAME` for class bodies; uncodable gaps stay checklist
`side_effects`.

## Usage (caller's view)

Packet authors (README / `docs/codemods.md`) declare structural edits with
one rule type:

```json
{
  "type": "AST_SHAPE_REWRITE",
  "target_files": ["*.py"],
  "shape": {
    "kind": "import_name",
    "module": "pydantic",
    "old_name": "validator",
    "new_name": "field_validator"
  }
}
```

and for nested config (names filled by the packet, not the engine):

```json
{
  "type": "AST_SHAPE_REWRITE",
  "shape": {
    "kind": "nested_assign",
    "class_name": "Config",
    "target_attr": "model_config",
    "constructor": "ConfigDict",
    "keys": {"orm_mode": "from_attributes"}
  }
}
```

**Apply** compiles all such rules once and discharges them per file:

```python
plan = compile_shape_plan(sdk_rules)
updated, n = apply_shape_plan_to_file(path, content, plan.for_path(path))
```

**Watch / leftovers** ask the same plan what is still present:

```python
leftovers += scan_shape_residual(root, files, compile_shape_plan(packet["rules"]))
```

**Mint** emits `AST_SHAPE_REWRITE` for the mechanical subset; unmapped
config keys stay `side_effects`. Consumers never pass package-specific
flags — only shape fields.

## Shape

**Data structures first.** `ImportNameShape` and `NestedAssignShape` are
the domain; `ShapeObligation` gives each an identity; `ShapePlan` is the
compiled set. Relocate policy is not a free string — `new_name is None` ⇒
DROP, `new_module` set ⇒ SPLIT, else IN_PLACE (`RelocateKind` derived).

**Load-bearing decisions:**

1. **One wire type (`AST_SHAPE_REWRITE`) with a closed `shape` discriminant**
   instead of extending opaque `old_import` or adding two sibling types.
   Module-path hops keep `AST_IMPORT_REWRITE` but lose short-identifier
   string fallback (CALL mop-up's `old in new` spirit applied; bare names
   never substring-replace).
2. **Apply and Watch share `ShapePlan`.** Residual identities are the same
   obligations apply discharges — dual verdict without a second matching
   policy (single source of truth per invariant).
3. **Nested class is one rule with a key map**, not N× `KEY_RENAME` plus
   prose. Unmapped body keys refuse full collapse and surface as residual
   (or mint-time `side_effects`); engine never invents successors.
4. **Split-import is SPLIT-by-default** when `new_module` is present —
   drop name from the old ImportFrom and merge into a new/existing
   ImportFrom for the target module. Mint that cannot name `new_module`
   leaves the gap in `side_effects` rather than half-applying.

**Validation at the boundary:** `parse_shape_rule` is the only reader of
the wire dict; libcst stays private inside `apply_shape_plan_to_file` /
`scan_shape_residual`. Public surface is three functions + frozen shapes —
CST merge/split/collapse is the depth behind that surface
(per boundary-discipline, interface depth).

**Deliberately out of scope for v1:** expanding binder completeness /
`SURFACE_DEFINITE_REWRITE` to import names or ClassDef. Shape residual is
the Watch oracle for these obligations; call-surface floors stay
CALL-only.

## Synthesis decision

(orchestrator fills)

## Tradeoffs accepted

- We accept a **new rule type** (and dual schema + stage + engine wiring)
  in exchange for not overloading `old_import` into three meanings.
- We accept **Watch growing beyond call-only leftovers** for shape
  residual in exchange for ending false-clean hops with dirty imports /
  Config.
- We accept **binder completeness remaining call/decorator-shaped in v1**
  in exchange for shipping the dual verdict without reshaping surface
  contracts / mint floors in the same change.
- We accept **SPLIT always when `new_module` is set** (no apply-time
  refuse) in exchange for encoding relocate in types; incomplete hops omit
  the field and use `side_effects`.
- We accept **leaving nested ClassDef in place when unmapped keys remain**
  in exchange for never deleting config the packet did not claim to
  migrate.

## Alternatives considered

- **Extend `AST_IMPORT_REWRITE` with optional name fields on the same
  opaque strings.** Loses: callers still face overloaded `old_import`
  semantics; string fallback temptation remains; nested config still needs
  a second invention. Shallower public story, more policy leakage into one
  string pair.
- **Two sibling types (`AST_IMPORT_NAME_REWRITE` +
  `AST_NESTED_CLASS_REWRITE`) with independent leftover arms.** Loses:
  two closed-set registrations, two residual policies that will drift;
  callers coordinate two concepts that share obligation/residual
  mechanics. Exposes stage wiring; hides less than one shape plan.
- **Compose Config from `KEY_RENAME` + `AST_PARAM_RENAME` +
  `side_effects`.** Loses: wrong AST targets (quoted keys / Call kwargs);
  schema-legal but semantically false `function_target: "Config"`;
  no ClassDef→Assign surgery. Pushes complexity onto packet authors.
- **Keep Watch call-only; fix apply only.** Loses: rubric dual verdict —
  dirty import after CALL rewrite stays CI-green. Rejected as repeating
  today's false-clean.

## Open questions and risks

- Should binder completeness eventually observe import-name and nested
  class obligations, or is shape residual enough forever?
- For SPLIT merges, how aggressive should same-module ImportFrom coalescing
  be when multiple relocate rules fire in one file?
- Is the package-agnostic mint heuristic that rejects
  `AST_PARAM_RENAME` with `function_target` in `{Config, Meta}` too sharp
  (false positives on real Call targets named Config)?
- Non-Python engines: ship import_name residual as Python-first and leave
  JS/TS/Java/Go shape apply as `NotImplemented` / skip, or gate
  `AST_SHAPE_REWRITE` to `.py` in schema `target_files` guidance only?

## Next implementation step

Add `$defs/ast_shape_rewrite` to both schema copies, register
`AST_SHAPE_REWRITE` in `SDK_RULE_TYPES`, and implement
`compile_shape_plan` + `ImportNameShape` apply/residual enough to replace
the pydantic hop fragment `old_import` with a real import_name shape and
prove idempotent re-apply on a multi-name ImportFrom line.
