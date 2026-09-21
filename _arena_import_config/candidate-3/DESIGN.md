# Candidate 3 — Residue catalog is the dual-verdict contract

Packet authors declare two new closed SDK rule types. Apply and Watch never re-parse those wire
dicts. A single decode step yields **rewrite ops** (what to change) and **residue specs** (what
"still old" means). The leftover gate and `conduit watch` scan the specs; apply executes the ops.
Binder completeness stays Call+decorator. Structural residue (import names, nested class shapes)
is a leftover kind, not a fake surface use.

This is not a point fix inside `leave_ImportFrom` plus a pydantic Config special case. The load-bearing move is: **`still_old` becomes a typed catalog shared by apply and Watch**, so "apply complete / Watch clean" cannot mean "calls rewritten, combined import and inner Config still dirty."

## Load-bearing decisions

1. **Sibling rule types, not overloaded strings.** Keep `AST_IMPORT_REWRITE` as *module-path only*.
   Name-level work is `AST_IMPORT_NAME_REWRITE` (module + old name + new name + optional new module).
   Nested class → assignment is `AST_NESTED_ASSIGN_REWRITE` (one rule, key map inside). Do not
   extend opaque `old_import`/`new_import`. Do not alias Config onto `AST_PARAM_RENAME` or `KEY_RENAME`.
2. **Split-import is an op, not a skip.** When `new_module` differs, drop the name from the source
   `ImportFrom` and merge it into a `from {new_module} import …` statement. Refuse only star-imports
   and unparseable trees.
3. **Leftover is tagged residue, not `callee: str`.** Kinds: `call | import_name | import_module | nested_shape | version_guard`. Watch JSON still prints a human line (CI greps keep working) and adds `kind`.
4. **Binder does not grow `UseKind`.** Completeness remains proof over Call+decorator observations.
   Dirty imports and leftover `class Config` fail the leftover arm, not `evaluate_binding`.
5. **No string fallback for names.** `validator` ⊂ `field_validator`. Name rewrite and nested-assign
   are libcst-only. Module-path `AST_IMPORT_REWRITE` may string-replace only when `old_import` is a
   dotted-module token **and** `old_import not in new_import`.
6. **Python-only for the new ops.** JS/TS/Java/Go keep module-path import rewrite. Non-`.py` targets
   for the new types are skips, not silent no-ops that Watch then calls clean.
7. **Mint promotes full-statement import blobs** into the name-level type. Fragments like
   `BaseModel, validator` are invalid. PascalCase bare `function_target` on `AST_PARAM_RENAME` is
   dropped (the Config footgun), not applied.

No `if package == "pydantic"` in core. Pydantic appears only in examples, smokes, and packet JSON.

---

## Module map

```
schema/conduit-packet.schema.json          # + $defs; dual copy under conduit/schema/
conduit/schema/conduit-packet.schema.json

conduit/packet/rewrite_ops.py              # NEW: wire → domain ops + residue specs (boundary)
conduit/packet/synthesize.py               # allowlist, promotion, PARAM_RENAME reject, prompts
conduit/packet/scope.py                    # old-token keys for the new types
conduit/packet/author.py                   # one-line rule summaries

conduit/patcher/rule_stages.py             # SDK_RULE_TYPES += both new types
conduit/patcher/engine.py                  # elif → typed apply; skip non-py
conduit/patcher/ast_import_rewrite.py      # module-path tightened; name/split walk
conduit/patcher/ast_nested_assign.py       # NEW: ClassDef → assignment + key map
conduit/patcher/residue.py                 # NEW: compile catalog, scan hits → Leftover
conduit/patcher/leftovers.py               # Leftover.kind; gate consumes residue scan
conduit/patcher/languages/python.py        # optional thin adapters; no second policy

conduit/watch.py                           # leftover tuple already shared; display + kind
conduit/surface/contracts.py               # UNCHANGED (CALL only)
conduit/surface/index.py                   # UNCHANGED (no Config as UseSite)
conduit/test_gen.py                        # do not oracle short import names as substrings
```

Ownership:

| Knowledge | Owner |
|-----------|--------|
| Wire shape, closed `oneOf`, mint aliases | schema + `synthesize.py` |
| "This dict is a well-typed rewrite op" | `packet/rewrite_ops.py` |
| CST surgery for imports | `ast_import_rewrite.py` |
| CST surgery for nested class → assign | `ast_nested_assign.py` |
| What still-old means after freeze | `patcher/residue.py` |
| Apply/Watch fail-close + binding compose | `leftovers.py` / `watch.py` |
| Proof-eligible CALL completeness | `surface/*` (untouched) |

Short call chain: packet JSON → `parse_rewrite_ops` → engine apply / `scan_residue_catalog`. Watch does not import libcst transformers.

---

## Packet wire (closed schema)

Both new types: `additionalProperties: false`, required fields as listed. Dual schema copies stay in lockstep.

### `AST_IMPORT_NAME_REWRITE` — rename (same module)

```json
{
  "type": "AST_IMPORT_NAME_REWRITE",
  "target_files": ["*.py"],
  "module": "pydantic",
  "old_name": "validator",
  "new_name": "field_validator",
  "reason": "Import alias walks the from-import name list; combined BaseModel, validator is in scope."
}
```

Matches `from pydantic import BaseModel, validator` and `from pydantic import validator as v`.
Renames `ImportAlias.name`; preserves `asname` unless `new_asname` is set.

### `AST_IMPORT_NAME_REWRITE` — split module

```json
{
  "type": "AST_IMPORT_NAME_REWRITE",
  "target_files": ["*.py"],
  "module": "pydantic",
  "old_name": "BaseSettings",
  "new_name": "BaseSettings",
  "new_module": "pydantic_settings",
  "reason": "Name moved packages; split the statement instead of rewriting the whole module path."
}
```

`from pydantic import BaseModel, BaseSettings` becomes two statements (merge into an existing
`pydantic_settings` import when present). Companion `DEPENDENCY_ADD` stays a separate rule.

### `AST_NESTED_ASSIGN_REWRITE` — inner class → assignment

```json
{
  "type": "AST_NESTED_ASSIGN_REWRITE",
  "target_files": ["*.py"],
  "inner_class": "Config",
  "assignment_target": "model_config",
  "constructor": "ConfigDict",
  "keys": { "orm_mode": "from_attributes" },
  "unmapped_keys": "refuse",
  "ensure_import": { "module": "pydantic", "name": "ConfigDict" },
  "reason": "Mechanical Config-shaped body → assignment; leftover kind nested_shape until the class is gone."
}
```

Optional `enclosing_base: "BaseModel"` restricts hits to nested classes whose enclosing `ClassDef`
bases resolve to that name (alias-aware, still not a package branch). Omitted → any enclosing class.

`unmapped_keys`: `refuse` (default, skip node, residue remains) | `keep` (pass through as kwargs) | `drop`.

Non-assign body (methods, nested classes, control flow) always refuses that node.

Uncodable key semantics (`allow_population_by_field_name` needing a different constructor, etc.)
stay in `side_effects`. The engine does not invent them.

### `AST_IMPORT_REWRITE` — unchanged wire, tighter semantics

```json
{
  "type": "AST_IMPORT_REWRITE",
  "target_files": ["*.py"],
  "old_import": "openai",
  "new_import": "openai"
}
```

`old_import` / `new_import` are **module paths**, not statements, not comma-clauses. Mint promotion
rewrites statement-shaped LLM output into `AST_IMPORT_NAME_REWRITE` before freeze.

### Schema `$defs` sketch

```json
"ast_import_name_rewrite": {
  "type": "object",
  "required": ["type", "target_files", "module", "old_name", "new_name"],
  "additionalProperties": false,
  "properties": {
    "type": { "const": "AST_IMPORT_NAME_REWRITE" },
    "target_files": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "module": { "type": "string", "minLength": 1 },
    "old_name": { "type": "string", "minLength": 1, "pattern": "^[A-Za-z_][A-Za-z0-9_]*$" },
    "new_name": { "type": "string", "minLength": 1, "pattern": "^[A-Za-z_][A-Za-z0-9_]*$" },
    "new_module": { "type": "string", "minLength": 1 },
    "new_asname": { "type": ["string", "null"] },
    "reason": { "$ref": "#/$defs/rule_reason" }
  }
},
"ast_nested_assign_rewrite": {
  "type": "object",
  "required": ["type", "target_files", "inner_class", "assignment_target", "constructor", "keys"],
  "additionalProperties": false,
  "properties": {
    "type": { "const": "AST_NESTED_ASSIGN_REWRITE" },
    "target_files": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "inner_class": { "type": "string", "minLength": 1 },
    "enclosing_base": { "type": "string", "minLength": 1 },
    "assignment_target": { "type": "string", "minLength": 1 },
    "constructor": { "type": "string", "minLength": 1 },
    "keys": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": { "type": "string", "minLength": 1 }
    },
    "unmapped_keys": { "type": "string", "enum": ["refuse", "keep", "drop"] },
    "ensure_import": { "$ref": "#/$defs/ensure_import" },
    "reason": { "$ref": "#/$defs/rule_reason" }
  }
},
"ensure_import": {
  "type": "object",
  "required": ["module", "name"],
  "additionalProperties": false,
  "properties": {
    "module": { "type": "string", "minLength": 1 },
    "name": { "type": "string", "minLength": 1 },
    "asname": { "type": "string", "minLength": 1 }
  }
}
```

Add both `$ref`s to `rule.oneOf`. Identifier patterns on `old_name` / `new_name` make comma-fragments
schema-invalid.

---

## Domain types (`conduit/packet/rewrite_ops.py`)

Wire dicts stop here. Patcher and residue import these types, not `rule["old_name"]`.

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Mapping


class UnmappedKeys(str, Enum):
    REFUSE = "refuse"
    KEEP = "keep"
    DROP = "drop"


@dataclass(frozen=True)
class EnsureImport:
    module: str
    name: str
    asname: str | None = None


@dataclass(frozen=True)
class InPlaceImportName:
    """Rename ImportAlias.name inside from {module} import …"""

    module: str
    old_name: str
    new_name: str
    new_asname: str | None = None
    target_files: tuple[str, ...] = ("*.py",)


@dataclass(frozen=True)
class SplitImportName:
    """Move one imported name to a different module; merge/split statements."""

    from_module: str
    to_module: str
    old_name: str
    new_name: str
    new_asname: str | None = None
    target_files: tuple[str, ...] = ("*.py",)


ImportNameOp = InPlaceImportName | SplitImportName


@dataclass(frozen=True)
class NestedAssignOp:
    inner_class: str
    assignment_target: str
    constructor: str
    keys: Mapping[str, str]
    unmapped: UnmappedKeys = UnmappedKeys.REFUSE
    enclosing_base: str | None = None
    ensure_import: EnsureImport | None = None
    target_files: tuple[str, ...] = ("*.py",)


ResidueKind = Literal[
    "call",
    "import_name",
    "import_module",
    "nested_shape",
    "version_guard",
]


@dataclass(frozen=True)
class ImportNameResidue:
    module: str
    old_name: str
    target_files: tuple[str, ...]
    rule_index: int


@dataclass(frozen=True)
class ImportModuleResidue:
    old_module: str
    target_files: tuple[str, ...]
    rule_index: int


@dataclass(frozen=True)
class NestedShapeResidue:
    inner_class: str
    enclosing_base: str | None
    target_files: tuple[str, ...]
    rule_index: int


@dataclass(frozen=True)
class CallResidue:
    old_callee: str
    target_files: tuple[str, ...]
    rule_index: int


@dataclass(frozen=True)
class ResidueCatalog:
    """Compiled still-old obligations. Apply mutates source; catalog does not."""

    calls: tuple[CallResidue, ...]
    import_names: tuple[ImportNameResidue, ...]
    import_modules: tuple[ImportModuleResidue, ...]
    nested_shapes: tuple[NestedShapeResidue, ...]


@dataclass(frozen=True)
class RewritePlan:
    import_names: tuple[ImportNameOp, ...]
    nested_assigns: tuple[NestedAssignOp, ...]
    # Existing AST_* stay on the engine elif path; they are not re-homed in this sketch.


def parse_import_name_rule(rule: Mapping[str, object]) -> ImportNameOp | None:
    """Validate-at-boundary. None → engine skip warning, never silent apply.

    Invariant: old_name and new_name are identifiers. If new_module is set and
    differs from module → SplitImportName; else InPlaceImportName.
    """
    raise NotImplementedError


def parse_nested_assign_rule(rule: Mapping[str, object]) -> NestedAssignOp | None:
    """Requires non-empty keys map; constructor and assignment_target are identifiers."""
    raise NotImplementedError


def parse_rewrite_plan(packet: Mapping[str, object]) -> RewritePlan:
    raise NotImplementedError


def residues_from_packet(packet: Mapping[str, object]) -> ResidueCatalog:
    """Single compile used by apply leftover gate and Watch.

    AST_CALL_REWRITE → CallResidue (old_callee)
    AST_IMPORT_NAME_REWRITE → ImportNameResidue (module, old_name)
    AST_IMPORT_REWRITE → ImportModuleResidue (old_import, only if it is a module token)
    AST_NESTED_ASSIGN_REWRITE → NestedShapeResidue (inner_class, enclosing_base)
    """
    raise NotImplementedError
```

`InPlaceImportName` vs `SplitImportName` encodes split policy in the type. Callers cannot forget
`new_module` and accidentally rewrite the source module path.

---

## Apply sketches

### Engine dispatch

```python
# conduit/patcher/engine.py  (inside _apply_rules_to_files)

from conduit.packet.rewrite_ops import (
    parse_import_name_rule,
    parse_nested_assign_rule,
)

# SDK_RULE_TYPES includes AST_IMPORT_NAME_REWRITE, AST_NESTED_ASSIGN_REWRITE
# deferred-impact set includes both

elif rule_type == "AST_IMPORT_NAME_REWRITE":
    op = parse_import_name_rule(rule)
    if op is None:
        report.skips.append(f"[{stage_label}] invalid AST_IMPORT_NAME_REWRITE")
        continue
    if path.suffix.lower() != ".py":
        report.skips.append(f"[{stage_label}] import-name rewrite is Python-only: {rel}")
        continue
    updated, count = rewrite_python_import_names(original, op)
    detail = f"[{stage_label}] Import name {op.old_name!r} ({count}x)"  # type: ignore[attr-defined]

elif rule_type == "AST_NESTED_ASSIGN_REWRITE":
    op = parse_nested_assign_rule(rule)
    if op is None:
        report.skips.append(f"[{stage_label}] invalid AST_NESTED_ASSIGN_REWRITE")
        continue
    if path.suffix.lower() != ".py":
        report.skips.append(f"[{stage_label}] nested-assign rewrite is Python-only: {rel}")
        continue
    updated, count = rewrite_python_nested_assign(original, op)
    detail = f"[{stage_label}] Nested {op.inner_class} → {op.assignment_target} ({count}x)"
```

`packet_has_call_site_rules` is renamed in leftovers to `packet_has_residue_rules` and stays
"any non-DEPENDENCY rule" so an import-name-only packet is not treated as pin-only.

### Import name CST (`ast_import_rewrite.py`)

Keep `_ImportRewriteTransformer` for **module paths**. Delete the lying comment. Do not walk
`names` on that transformer.

```python
def rewrite_python_import_names(content: str, op: ImportNameOp) -> tuple[str, int]:
    """AST only. No str.replace. Idempotent: already-new names are left alone.

    Fast-path: if neither module nor old_name appears in content, return (content, 0).
    Unparseable: return (content, 0) — leftover scan will still see the text via ast.parse fail
    as a skip, not a string smash.
    """
    raise NotImplementedError


class _ImportNameTransformer(cst.CSTTransformer):
    """leave_ImportFrom only. import x as y is out of scope (that's AST_IMPORT_REWRITE)."""

    def leave_ImportFrom(self, original_node, updated_node):
        # TODO match dotted module == op.module (exact; relative imports: module string only)
        # TODO if names is ImportStar: mark refused_star; return unchanged
        # TODO walk ImportAlias:
        #   match alias.name == old_name (not asname, unless asname is the only local
        #   and packet old_name equals it — still match .name, preserve asname)
        # InPlace: alias.with_changes(name=Name(new_name), asname=…)
        # Split: drop alias from this statement; queue pending insert
        # Empty names after drop → RemovalSentinel
        raise NotImplementedError

    def leave_Module(self, original_node, updated_node):
        # TODO for each pending Split import: merge into existing ImportFrom(to_module)
        #      else insert after the original statement's original location
        # TODO EnsureImport is not this transformer
        raise NotImplementedError
```

Split merge rules (invariants):

- Duplicate `from to_module import NewName` already present → drop from source only (count as 1).
- After drop, source `ImportFrom` with zero names → remove statement.
- Re-apply: source no longer has `old_name` from `from_module` → 0 changes (idempotent).
- `from pydantic import *` matching `module` → 0 changes; residue scan flags star as leftover.

Module-path fallback change:

```python
_MODULE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

def rewrite_python_imports(content, old_import, new_import):
    # AST pass unchanged (module / prefix only)
    # Fallback ONLY if:
    #   _MODULE_TOKEN.fullmatch(old_import)
    #   and old_import not in new_import
    #   and transformer.changes == 0
    # Comma fragments and full `from x import y` strings never hit str.replace.
    raise NotImplementedError
```

### Nested assign CST (`ast_nested_assign.py`)

```python
def rewrite_python_nested_assign(content: str, op: NestedAssignOp) -> tuple[str, int]:
    raise NotImplementedError


class _NestedAssignTransformer(cst.CSTTransformer):
    def __init__(self, op: NestedAssignOp) -> None:
        self.op = op
        self.changes = 0
        self._class_stack: list[str] = []
        self._bases_stack: list[tuple[str, ...]] = []

    def visit_ClassDef(self, node):
        # TODO push name + dotted bases (Name / Attribute only)
        raise NotImplementedError

    def leave_ClassDef(self, original_node, updated_node):
        # TODO pop
        # TODO if updated_node.name.value != op.inner_class: return
        # TODO if not nested (stack empty after pop of self): return  # top-level Config stays
        # TODO if op.enclosing_base and it is not in enclosing bases: return
        # TODO body_to_kwargs(body):
        #   simple Assign of Name = (Name|Integer|Float|SimpleString|Name True/False/None)
        #   any other statement → refuse whole node
        # TODO remap keys; unmapped per op.unmapped
        # TODO replace ClassDef with SimpleStatementLine:
        #   assignment_target = constructor(new_key=value, ...)
        # Preserve leading comments attached to the class when libcst allows; do not
        # reformat the rest of the enclosing class.
        raise NotImplementedError

    def leave_Module(self, original_node, updated_node):
        # TODO if op.ensure_import and constructor name not already imported, merge ImportFrom
        raise NotImplementedError
```

Idempotency: if the inner class is gone and `assignment_target = constructor(...)` already exists,
0 changes. If both still exist, rewrite the class (leftover would otherwise stay dirty). Do not
rewrite an existing assignment's kwargs on a second pass unless the inner class is still present
(avoids fighting hand-edits).

Generic: `inner_class`, `assignment_target`, `constructor`, `keys` are packet data. The transformer
never mentions pydantic.

---

## Leftover / Watch contract

Today `Leftover.callee` is a call-shaped string and `scan_leftovers` only matches
`AST_CALL_REWRITE.old_callee`. Completeness can be `complete` with a dirty combined import and a
live `class Config`. That is the dual-verdict lie this candidate deletes.

### Types

```python
# conduit/patcher/leftovers.py

@dataclass(frozen=True)
class Leftover:
    kind: ResidueKind
    rel: str
    token: str
    reason: str
    lineno: int = 0

    def display(self) -> str:
        loc = f"{self.rel}:{self.lineno}" if self.lineno else self.rel
        return f"{loc}  {self.token}  ({self.kind}: {self.reason})"
```

`callee` is gone. Call leftovers set `kind="call"` and `token=callee`. Watch `to_dict()`:

```python
"leftover_count": len(self.leftovers),
"leftovers": [item.display() for item in self.leftovers],
"leftover_kinds": sorted({item.kind for item in self.leftovers}),
```

Display still contains `validator` / `Config` so existing CI string checks keep working if packets
keep those rules. Smokes that *assert Config residue + Watch clean* must change when the packet
gains `AST_NESTED_ASSIGN_REWRITE`.

### Scan (`conduit/patcher/residue.py`)

```python
def scan_residue_catalog(
    *,
    root: Path,
    files: Sequence[Path],
    catalog: ResidueCatalog,
    calls: Sequence[PackageCall],
) -> list[Leftover]:
    """Pure. No writes. Shared by evaluate_apply_leftovers and evaluate_watch."""
    raise NotImplementedError


def _scan_python_structural(rel: str, source: str, catalog: ResidueCatalog) -> list[Leftover]:
    # ast.parse; walk:
    # ImportFrom: module match + (alias.name == old_name OR ImportStar)
    #   → Leftover(kind="import_name", token=f"{module}.{old_name}")
    # Import / ImportFrom module == ImportModuleResidue.old_module
    #   → kind="import_module"
    # ClassDef nested named inner_class (optional enclosing_base via parent walk)
    #   → kind="nested_shape", token=inner_class
    # Do not flag assignment_target=constructor as leftover (that's the new form).
    raise NotImplementedError
```

Call arm: keep today's `callee == old or endswith("." + old)` over `collect_package_calls`. This
arena does not retune CALL matching.

`scan_packet_leftovers`:

```python
def scan_packet_leftovers(root: Path, packet: dict) -> list[Leftover]:
    catalog = residues_from_packet(packet)
    # existing file prune via dependency_packages / prune_by_imports
    calls = collect_package_calls(...)  # as today
    found = scan_residue_catalog(root=root, files=files, catalog=catalog, calls=calls)
    # version-guard arm unchanged, kind="version_guard"
    return found
```

Apply gate: any leftover kind → `status="dirty"` exit 1. Binding still runs after leftovers clear.
Watch: pin ≥ to_version + any leftover kind → `bump_dirty`. Completeness only after leftovers empty.

**Invariant:** if apply reported a rewrite count of 0 for an `AST_IMPORT_NAME_REWRITE` and the name
is still imported, Watch reports `import_name`. There is no third scanner.

### What Watch does *not* do

- Does not treat unused-but-new aliases as leftovers.
- Does not observe nested Config as a binder `UseSite`.
- Does not execute `side_effects`.
- Does not substring-search `old_name` in file text (`validator` inside `field_validator`).

### Completeness vs leftovers

| Question | Owner | Grows in this design? |
|----------|--------|------------------------|
| Was this CALL/decorator surface observed and rewritten? | binder | No |
| Does a packet-declared old import name / module / inner class still exist? | residue leftovers | Yes |
| Uncodable multi-statement gap? | `side_effects` checklist | No |

---

## Mint / normalize

```python
# synthesize.py additions

_LLM_RULE_ALLOWED_KEYS["AST_IMPORT_NAME_REWRITE"] = frozenset({
    "type", "target_files", "module", "old_name", "new_name",
    "new_module", "new_asname", "reason",
})
_LLM_RULE_ALLOWED_KEYS["AST_NESTED_ASSIGN_REWRITE"] = frozenset({
    "type", "target_files", "inner_class", "enclosing_base",
    "assignment_target", "constructor", "keys", "unmapped_keys",
    "ensure_import", "reason",
})
_LLM_RULE_FIELD_ALIASES["AST_IMPORT_NAME_REWRITE"] = ("old_name", "new_name")

def normalize_llm_rule(rule: dict[str, Any]) -> dict[str, Any]:
    # existing alias mapping …
    rule = _promote_statement_import(rule)
    rule = _drop_class_shaped_param_rename(rule)
    # strip to allowlist
    raise NotImplementedError


def _promote_statement_import(rule: dict) -> dict:
    """If type is AST_IMPORT_REWRITE and old_import/new_import parse as ImportFrom
    of the form `from M import N` / `from M2 import N2` (single name, no star):
    emit AST_IMPORT_NAME_REWRITE {module: M, old_name: N, new_name: N2, new_module: M2 or omit}.
    Multi-name statements are not promoted (author must emit one rule per name).
    Comma fragments without `from` / `import` keywords: leave type but they fail schema
    if someone later types them as NAME_REWRITE; for IMPORT_REWRITE they fail the module-token
    fallback and apply 0 — leftover import_module will not fire (not a module token).
    Mint prompts must not emit fragments.
    """
    raise NotImplementedError


def _drop_class_shaped_param_rename(rule: dict) -> dict:
    """AST_PARAM_RENAME whose function_target is a single PascalCase identifier is not a call
    chain. Drop the rule (return a tombstone the list filter removes). Generic, not pydantic.
    """
    raise NotImplementedError
```

Prompt delta (`_EVIDENCE_SYSTEM` and `complete_json` instructions):

- Allowed types += `AST_IMPORT_NAME_REWRITE`, `AST_NESTED_ASSIGN_REWRITE`.
- `AST_IMPORT_REWRITE` is module paths only. Combined `from x import A, B` name hops use
  `AST_IMPORT_NAME_REWRITE` (one name per rule).
- Inner-class config blocks that are a class of simple assigns + key renames + a constructor
  assignment use `AST_NESTED_ASSIGN_REWRITE`. Methods / uncodable keys stay `side_effects`.
- Never `AST_PARAM_RENAME` / `KEY_RENAME` for unquoted class-body assigns.

`scope.py` `_OLD_TOKEN_KEYS`:

```python
"AST_IMPORT_NAME_REWRITE": "old_name",  # plus module via _rule_old_tokens extra
"AST_NESTED_ASSIGN_REWRITE": "inner_class",
```

`_rule_old_tokens` also yields `module` and each `keys` old key so source-usage filter keeps
Config rules when the client tree mentions them.

`test_gen.oracle_forbidden_tokens`: do **not** add `old_name` (short identifiers). Residue leftover
scan is the oracle for names. Keep `old_import` only when it is a module token.

---

## Dual-verdict story (pydantic fixture)

Source:

```python
from pydantic import BaseModel, validator

class User(BaseModel):
    @validator("name")
    def check_name(cls, v):
        return v
    class Config:
        orm_mode = True
```

Packet (mechanical subset): `DEPENDENCY_BUMP` + `AST_IMPORT_NAME_REWRITE` (validator→field_validator)
+ `AST_CALL_REWRITE` (validator→field_validator) + `AST_NESTED_ASSIGN_REWRITE` (Config →
`model_config = ConfigDict(from_attributes=True)` + `ensure_import ConfigDict`). `.dict()` and
classmethod reshape stay `side_effects`.

| Stage | Leftovers | Binder |
|-------|-----------|--------|
| Pre-apply, pin at 2.0 | call `validator`, import_name `pydantic.validator`, nested_shape `Config` | incomplete/unverified as today if surfaces proof-eligible |
| Post-apply success | empty | complete if CALL surfaces observed |
| Apply missed combined import | import_name remains → apply exit 1, Watch `bump_dirty` | not consulted |
| Packet omits nested rule (Config in side_effects only) | Config is **not** leftover | Watch can be clean with `class Config` still in tree |

That last row is packet-driven honesty: Watch only claims mechanical residue the packet declared.
Existing hop smoke that asserts Config residue must either keep Config in `side_effects` (Watch
blind to it, documented) or adopt the nested rule and assert Config is gone.

Re-apply: 0 CST hits; leftover scan still empty.

---

## Engine registry / stages

```python
SDK_RULE_TYPES |= {
    "AST_IMPORT_NAME_REWRITE",
    "AST_NESTED_ASSIGN_REWRITE",
}
```

Do not put nested-assign in REST (`KEY_RENAME` stays quoted JSON/YAML/env). Do not add a
`SURFACE_DEFINITE_REWRITE` path for ClassDef — synthetic surface pass remains Call-only.

---

## Deliberately not done

- No `UseKind.IMPORT` / `UseKind.NESTED_CLASS` on the binder.
- No `leave_ImportFrom` name walk bolted onto today's opaque-string transformer.
- No string mop-up for nested assigns (`orm_mode` ⊂ nothing useful; still wrong to substring).
- No auto `DEPENDENCY_ADD` when `new_module` is set — packet must declare it (frozen apply).
- No JS named-import rewrite in this sketch (language engines stay module-path).
- No half-rewrite of inner classes that contain methods; leftover `nested_shape` remains until a
  human or a later packet rule handles them.
