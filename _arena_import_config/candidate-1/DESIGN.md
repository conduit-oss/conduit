# Candidate 1 — Shape obligations (one rule, residual oracle)

**Primary axis:** import-list + nested-config are the same kind of thing —
packet-declared **structural obligations** — not two bolt-ons onto opaque
`old_import` strings and call-only leftovers.

Wire gets **one** new SDK rule type `AST_SHAPE_REWRITE` with a closed
`shape` discriminant (`import_name` | `nested_assign`). Apply compiles
shapes into a `ShapePlan`; Watch reports undischarged obligations from the
**same** plan vocabulary. `AST_IMPORT_REWRITE` stays module-path-only and
loses its unguarded short-identifier string fallback.

No `if package == "pydantic"` anywhere in core. Pydantic appears only in
packet examples / smokes that *fill* generic fields.

---

## Usage (caller's view) — write this first

### README / docs/codemods.md

```markdown
### AST_SHAPE_REWRITE

Declares a structural edit the engine can discharge atomically:
rename/drop/relocate a name inside `from … import …`, or rewrite a nested
class body into an assignment with a key map. Watch leftovers include any
obligation still present after apply.

Import name (same module):

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

Nested class → assignment (generic; packet fills names):

{
  "type": "AST_SHAPE_REWRITE",
  "target_files": ["*.py"],
  "shape": {
    "kind": "nested_assign",
    "class_name": "Config",
    "target_attr": "model_config",
    "constructor": "ConfigDict",
    "keys": {"orm_mode": "from_attributes"}
  },
  "reason": "Inner Config becomes model_config = ConfigDict(...)."
}
```

Module-path hops keep using `AST_IMPORT_REWRITE` (`old_import` /
`new_import` = dotted modules). Do **not** put name lists or statement
fragments in those fields.

Uncodable nested keys (no mechanical successor) stay in `side_effects`
with `kind: "config"`. Never invent `AST_PARAM_RENAME` with
`function_target: "Config"` for class bodies.

### Call site 1 — apply (`patcher/engine.py`)

```python
from conduit.patcher.shape_rewrite import (
    compile_shape_plan,
    apply_shape_plan_to_file,
)

# inside _apply_rules_to_files, once per SDK pass (not per-rule elif sprawl):
plan = compile_shape_plan(sdk_rules)  # filters AST_SHAPE_REWRITE only
# ... existing per-rule loop for DEPENDENCY_*, AST_IMPORT_REWRITE (module),
#     AST_CALL_REWRITE, etc. ...
elif rule_type == "AST_SHAPE_REWRITE":
    # prefer: batch apply outside the loop; if kept in-loop, parse once:
    updated, count = apply_shape_plan_to_file(path, original, plan.for_path(path))
```

Preferred (deeper) orchestration — one plan, one visit per file:

```python
shape_plan = compile_shape_plan(sdk_rules)
for path in files:
    original = read(path)
    updated, n = apply_shape_plan_to_file(path, original, shape_plan.for_path(path))
    # record ChangeRecords from n / plan hits
```

### Call site 2 — leftovers / Watch (`patcher/leftovers.py`, `watch.py`)

```python
from conduit.patcher.shape_rewrite import compile_shape_plan, scan_shape_residual

plan = compile_shape_plan(packet.get("rules") or [])
shape_hits = scan_shape_residual(root, files, plan)
# merge with existing CALL leftovers; same Leftover display type, richer reason
leftovers = call_leftovers + shape_hits
```

`evaluate_watch` / `evaluate_apply_leftovers` do not grow a second policy:
residual presence **is** dirty. Binder completeness may still be
call/decorator-only in v1; shape residual is the Watch-visible oracle for
import names + nested class (see Tradeoffs).

### Call site 3 — mint normalize (`packet/synthesize.py`)

```python
# _LLM_RULE_ALLOWED_KEYS gains AST_SHAPE_REWRITE
# normalize_llm_rule accepts shape dict; rejects fake Config-via-PARAM
# _EVIDENCE_SYSTEM: mechanical nested Config + key map → AST_SHAPE_REWRITE;
#                   unmapped keys → side_effects kind=config
```

Hand-authored pydantic hop packet replaces fragment `old_import` with:

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

plus the existing `AST_CALL_REWRITE` for `@validator` → `@field_validator`.

---

## Module map

| Concern | Path | Owns |
|--------|------|------|
| Wire schema `$defs/ast_shape_rewrite` | `schema/conduit-packet.schema.json` + dual `conduit/schema/` | Closed `shape` oneOf; `additionalProperties: false` |
| Domain types + compile | `conduit/patcher/shape_rewrite.py` | Parse wire → `ShapePlan`; apply; residual scan |
| CST surgery (Python) | same module, private transformers | `ImportAlias` walk; `ClassDef` → `Assign` |
| Stage set | `conduit/patcher/rule_stages.py` | Add `AST_SHAPE_REWRITE` to `SDK_RULE_TYPES` |
| Engine dispatch | `conduit/patcher/engine.py` | One branch / preferred batch apply |
| Module-path import (narrowed) | `conduit/patcher/ast_import_rewrite.py` | Module only; **no** unguarded short-id fallback |
| Leftover merge | `conduit/patcher/leftovers.py` | `scan_packet_leftovers` += shape residual |
| Mint allowlist / prompts | `conduit/packet/synthesize.py` | Keys, evidence prompt reverse, PARAM Config forbid |
| Scope tokens | `conduit/packet/scope.py` | `_OLD_TOKEN_KEYS["AST_SHAPE_REWRITE"]` → derived old tokens |
| Docs / smoke | `docs/codemods.md`, pydantic hop packet | Examples only; no core package branches |

**Does not touch:** `KEY_RENAME` (quoted REST), `AST_PARAM_RENAME` (Call kwargs),
`SURFACE_DEFINITE_REWRITE` (Call contracts), binder `contracts_from_packet`
(still CALL-only). Shape residual is orthogonal to surface floors.

---

## Packet wire examples

### Import name — rename in place (aliases preserved)

```json
{
  "type": "AST_SHAPE_REWRITE",
  "target_files": ["*.py"],
  "shape": {
    "kind": "import_name",
    "module": "pydantic",
    "old_name": "validator",
    "new_name": "field_validator"
  },
  "reason": "from pydantic import BaseModel, validator → …, field_validator"
}
```

Source `from pydantic import BaseModel, validator as v` →
`from pydantic import BaseModel, field_validator as v` (asname kept unless
`asname` field overrides).

### Import name — relocate (split statement)

```json
{
  "type": "AST_SHAPE_REWRITE",
  "target_files": ["*.py"],
  "shape": {
    "kind": "import_name",
    "module": "pydantic",
    "old_name": "BaseSettings",
    "new_name": "BaseSettings",
    "new_module": "pydantic_settings"
  }
}
```

Policy encoded in types: when `new_module` is set and ≠ `module`, edit is
`RelocateSplit` — drop name from old `ImportFrom`; insert/merge
`from pydantic_settings import BaseSettings` (see types). No refuse-at-apply;
mint may omit `new_module` and put the gap in `side_effects` instead.

### Import name — drop

```json
{
  "shape": {
    "kind": "import_name",
    "module": "pkg",
    "old_name": "Gone",
    "new_name": null
  }
}
```

`new_name: null` ⇒ remove alias; if names empty after drop ⇒ remove statement.

### Nested assign + key map

```json
{
  "type": "AST_SHAPE_REWRITE",
  "target_files": ["*.py"],
  "shape": {
    "kind": "nested_assign",
    "class_name": "Config",
    "target_attr": "model_config",
    "constructor": "ConfigDict",
    "keys": {
      "orm_mode": "from_attributes",
      "allow_population_by_field_name": "populate_by_name"
    },
    "ensure_constructor_import": {
      "module": "pydantic",
      "name": "ConfigDict"
    }
  }
}
```

Transforms:

```python
class User(BaseModel):
    class Config:
        orm_mode = True
```

into:

```python
class User(BaseModel):
    model_config = ConfigDict(from_attributes=True)
```

(plus `ConfigDict` import via companion `import_name` / ensure clause).

Keys present in the nested body but **absent** from `keys` map: do not
guess — leave residual nested class **or** (mint-time) emit
`side_effects`. Apply policy: if any unmapped Assign remains in body after
key rewrite, **do not delete** the class; record residual. If all body
Assigns mapped and no leftover stmts, replace ClassDef with Assign.

---

## Schema sketch (`$defs`)

```json
"ast_shape_rewrite": {
  "type": "object",
  "required": ["type", "target_files", "shape"],
  "additionalProperties": false,
  "properties": {
    "type": { "const": "AST_SHAPE_REWRITE" },
    "target_files": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "shape": {
      "oneOf": [
        { "$ref": "#/$defs/shape_import_name" },
        { "$ref": "#/$defs/shape_nested_assign" }
      ]
    },
    "reason": { "$ref": "#/$defs/rule_reason" }
  }
},
"shape_import_name": {
  "type": "object",
  "required": ["kind", "module", "old_name"],
  "additionalProperties": false,
  "properties": {
    "kind": { "const": "import_name" },
    "module": { "type": "string", "minLength": 1 },
    "old_name": { "type": "string", "minLength": 1 },
    "new_name": { "type": ["string", "null"] },
    "new_module": { "type": "string", "minLength": 1 },
    "asname": { "type": ["string", "null"] }
  }
},
"shape_nested_assign": {
  "type": "object",
  "required": ["kind", "class_name", "target_attr", "constructor", "keys"],
  "additionalProperties": false,
  "properties": {
    "kind": { "const": "nested_assign" },
    "class_name": { "type": "string", "minLength": 1 },
    "target_attr": { "type": "string", "minLength": 1 },
    "constructor": { "type": "string", "minLength": 1 },
    "keys": {
      "type": "object",
      "additionalProperties": { "type": "string", "minLength": 1 },
      "minProperties": 1
    },
    "outer_base": { "type": "string" },
    "ensure_constructor_import": {
      "type": "object",
      "required": ["module", "name"],
      "additionalProperties": false,
      "properties": {
        "module": { "type": "string" },
        "name": { "type": "string" }
      }
    }
  }
}
```

Add `{ "$ref": "#/$defs/ast_shape_rewrite" }` to `rules[].oneOf`.

**Narrow `AST_IMPORT_REWRITE` docs** (schema strings unchanged for back-compat):
semantics = dotted **module** paths only. Mint prompts forbid statement /
comma fragments.

---

## Python type / signature sketch

```python
# conduit/patcher/shape_rewrite.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

class RelocateKind(Enum):
    """Encoded by presence of new_module — not a free string policy."""
    IN_PLACE = "in_place"       # new_module is None
    SPLIT = "split"             # new_module set and != module
    DROP = "drop"               # new_name is None


@dataclass(frozen=True)
class ImportNameShape:
    module: str
    old_name: str
    new_name: str | None          # None ⇒ DROP
    new_module: str | None        # set ⇒ SPLIT (must ≠ module)
    asname: str | None            # None ⇒ preserve existing asname

    def relocate(self) -> RelocateKind:
        if self.new_name is None:
            return RelocateKind.DROP
        if self.new_module and self.new_module != self.module:
            return RelocateKind.SPLIT
        return RelocateKind.IN_PLACE

    def __post_init__(self) -> None:
        if self.new_module == self.module:
            raise ValueError("new_module must differ from module or be omitted")


@dataclass(frozen=True)
class NestedAssignShape:
    class_name: str
    target_attr: str
    constructor: str              # bare or dotted ctor name in Assign RHS
    keys: Mapping[str, str]       # old_key → new_key; immutable
    outer_base: str | None        # optional: only under ClassDef with this base
    ensure_import_module: str | None
    ensure_import_name: str | None


Shape = ImportNameShape | NestedAssignShape


@dataclass(frozen=True)
class ShapeObligation:
    """Unit of residual: what Watch still sees if apply did not discharge it."""
    kind: Literal["import_name", "nested_assign"]
    identity: str                 # stable: "pydantic::validator" or "Config@model_config"
    shape: Shape
    target_globs: tuple[str, ...]


@dataclass(frozen=True)
class ShapePlan:
    obligations: tuple[ShapeObligation, ...]

    def for_path(self, path: Path) -> ShapePlan:
        raise NotImplementedError  # filter by target_files globs

    @property
    def empty(self) -> bool:
        return not self.obligations


def parse_shape_rule(rule: Mapping[str, Any]) -> ShapeObligation:
    """Wire → domain. Only place packet dict is read for shapes.
    Raises ShapeRuleError on malformed / contradictory fields.
    """
    raise NotImplementedError


def compile_shape_plan(rules: Sequence[Mapping[str, Any]]) -> ShapePlan:
    """Filter AST_SHAPE_REWRITE; parse each; dedupe by obligation.identity.
    Conflicting same-identity different targets → ShapeRuleError (fail mint/apply).
    """
    raise NotImplementedError


def apply_shape_plan_to_file(
    path: Path,
    content: str,
    plan: ShapePlan,
) -> tuple[str, int]:
    """One libcst pass applying all obligations targeting this file.
    Idempotent: second apply with discharged shapes → 0 changes.
    No str.replace fallback for import names (validator ⊂ field_validator).
    """
    raise NotImplementedError


def scan_shape_residual(
    root: Path,
    files: Sequence[Path],
    plan: ShapePlan,
) -> list[Any]:  # list[Leftover] — avoid circular import; same display contract
    """AST observe: ImportFrom still harbors old_name under module;
    nested ClassDef class_name still has any keys.keys() or class still present
    when plan expected full collapse.
    """
    raise NotImplementedError
```

### CST transformers (private; not public API)

```python
class _ImportNameTransformer(cst.CSTTransformer):
    """leave_ImportFrom: walk ImportAlias names.
    IN_PLACE: rename .name, keep/override .asname
    DROP: filter alias out; RemovalSentinel if empty
    SPLIT: drop from this node; stash InsertImport for module-level merge
    Never matches on substring of other aliases.
    """
    ...


class _NestedAssignTransformer(cst.CSTTransformer):
    """leave_ClassDef (nested only — parent stack tracks outer ClassDef):
    if name == class_name and (outer_base is None or outer bases match):
      collect Assign targets in body matching keys
      if unmapped Assign/stmt remains → leave class, count residual flag
      else replace nested ClassDef with:
        SimpleStatementLine(Assign(target_attr, Call(constructor, kwargs=renamed)))
    ensure_import: sibling InsertImport merged post-visit
    """
    ...
```

### Narrowed module-path import

```python
# ast_import_rewrite.py — rewrite_python_imports
def rewrite_python_imports(content: str, old_import: str, new_import: str) -> tuple[str, int]:
    """Module-path only.
    String fallback ONLY when old_import contains '.' (dotted module) AND
    not (old_import in new_import) — same guard spirit as CALL mop-up.
    Bare identifiers / comma fragments: AST miss ⇒ 0 changes (no replace).
    """
    raise NotImplementedError  # replace body; keep signature for registry
```

### Engine / stages / leftovers

```python
# rule_stages.py
SDK_RULE_TYPES = frozenset({
    ...,
    "AST_SHAPE_REWRITE",
})

# leftovers.py — scan_packet_leftovers
def scan_packet_leftovers(root: Path, packet: dict) -> list[Leftover]:
    call_hits = ...  # existing
    plan = compile_shape_plan(packet.get("rules") or [])
    shape_hits = scan_shape_residual(root, files, plan)
    return call_hits + shape_hits

# Leftover.reason examples:
#   "import_name still present"
#   "nested_assign class still present"
#   "nested_assign unmapped key orm_mode"
```

### Mint normalize

```python
_LLM_RULE_ALLOWED_KEYS["AST_SHAPE_REWRITE"] = frozenset({
    "type", "target_files", "shape", "reason",
})

def _forbid_fake_config_param(rule: dict) -> dict:
    """If AST_PARAM_RENAME and function_target looks like a nested class name
    with no Call evidence requirement — drop or coerce to side_effects note.
    Conservative: function_target in {"Config", "Meta"} + old_param in common
    config keys → reject from rules, push side_effects. Package-agnostic
    heuristic on *shape*, not package string.
    """
    raise NotImplementedError
```

Scope tokens: for `import_name`, token = `old_name`; for `nested_assign`,
tokens = `class_name` + each `keys` old key (for generated oracle tests).

---

## Apply / Watch data flow

```
frozen packet
    │
    ├─ AST_IMPORT_REWRITE ──► module-path transformer (narrowed fallback)
    ├─ AST_CALL_REWRITE ────► call transformer + CALL leftovers (unchanged)
    └─ AST_SHAPE_REWRITE ──► compile_shape_plan
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
           apply_shape_plan_to_file   scan_shape_residual
           (ImportAlias / ClassDef)   (same identities)
                    │                       │
                    └───────────┬───────────┘
                                ▼
                     leftover gate / Watch dirty
```

**Invariant:** an obligation identity that apply can discharge is the same
identity residual scans. No second string policy for "is this import still
old?" (single source of truth per invariant).

**Idempotency:** IN_PLACE rename of `validator` → `field_validator` matches
only alias `.name == old_name`, never substring of `field_validator`.
Nested assign: after collapse, no `class Config` remains ⇒ residual empty;
re-apply finds nothing.

---

## What this design deliberately does not do

- Does not overload `AST_PARAM_RENAME` / `KEY_RENAME` for class-body assigns.
- Does not extend opaque `old_import` to mean "maybe module, maybe fragment,
  maybe name" — that leak is the hop-smoke workaround being retired.
- Does not put libcst nodes on the public `ShapePlan` API.
- Does not require binder completeness to understand shapes in v1 (Watch
  residual is sufficient dual verdict for these obligations).
- Does not branch on `packet["package"]`.

---

## Red-flag self-screen

| Flag | Verdict |
|------|---------|
| Shallow module | Public surface is three functions + two frozen shapes. CST merge/split/collapse lives behind `apply_shape_plan_to_file`. |
| Information leakage | Wire `shape` dict parsed once at `parse_shape_rule`. Leftovers consume `ShapePlan`, not raw rule dicts. Module-path import no longer shares "string means name" with shapes. |
| Temporal decomposition | Compile / apply / residual share one obligation vocabulary — knowledge ownership is "structural shape", not pipeline stage. |
| Pass-through | No `apply_shape_rewrite(path, content, rule_dict)` forwarding into identical args; engine either batches `ShapePlan` or parses through `parse_shape_rule` once. |

---

## First implementation slice (post-synthesis)

1. Schema + `SDK_RULE_TYPES` + `compile_shape_plan` / `ImportNameShape` apply path.
2. Kill short-id string fallback on `AST_IMPORT_REWRITE`.
3. Swap pydantic hop packet fragment → `AST_SHAPE_REWRITE` import_name; assert multi-name line + idempotent re-apply.
4. Nested assign transformer + residual; extend hop side_effects → mechanical keys in shape, residual keys in side_effects.
5. Wire `scan_shape_residual` into `scan_packet_leftovers`.
