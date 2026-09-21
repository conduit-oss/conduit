# Candidate 4 — Symbol-identity ops + one executor, two modes

**Thesis.** Both moves are the same engine problem: *a packet asserts a change to a
declaration, and the engine owns every statement that carries it.* Today the packet
asserts **text** (`old_import` strings) and the engine owns only *some* of the statements
(module paths), so the wire has to lie (clause fragments) and a second, drifting oracle
(call-only leftovers) decides whether the lie worked.

This candidate:

1. Replaces opaque import strings with a **symbol identity** rule
   (`AST_SYMBOL_MOVE`, `module:name` on both sides) that owns the import clause, the
   alias, the split when the module changes, and the unaliased use sites.
2. Adds a **declaration reshape** rule (`AST_NESTED_CLASS_TO_ASSIGN`) that turns a nested
   config-shaped class into `target = Wrapper(...)` with a key map, all-or-nothing per
   class, unmapped keys governed by a policy enum in the rule.
3. Introduces one **lowering** step (wire dicts → typed ops) and one **executor with two
   modes** (`APPLY` / `PROBE`). Leftovers for AST rules stop being a hand-written oracle
   and become "run the op, discard the edit, report that it *would* have changed
   something." Apply's post-gate and Watch consume the same residues.

No `if package == ...` anywhere: `pydantic:validator`, `Config`, `orm_mode` are packet data.

---

## 1. Module map

```
conduit/packet/
  lower.py            NEW  wire rule dicts -> Plan(ops, diagnostics); legacy desugar
  synthesize.py       EDIT allowlist + prompts + mint-time upgrade of import rules
  scope.py            EDIT token extraction becomes a function, not a dict of key names

conduit/patcher/
  ops/__init__.py     NEW  Op protocol, OpOutcome, Residue, op registry, run_plan()
  ops/symbol_move.py  NEW  SymbolMoveOp
  ops/nested_class.py NEW  NestedClassToAssignOp
  ops/module_rename.py NEW (internal only) today's honest module-path rewrite
  py/unit.py          NEW  FileUnit: rel, source, lazily parsed MetadataWrapper
  py/imports.py       NEW  ImportLedger  <-- the deep module: all Import/ImportFrom surgery
  engine.py           EDIT dispatch lowered ops; extend deferred-path skip set
  rule_stages.py      EDIT SDK_RULE_TYPES += the two new types
  leftovers.py        EDIT residue arm + residues_to_leftovers(); post-apply idempotency check
  ast_import_rewrite.py EDIT delete the unguarded string fallback; keep module-path engine

conduit/watch.py      EDIT WatchVerdict gains `advisories` (additive JSON)
conduit/test_gen.py   EDIT forbidden-token oracle: word-boundary match, new token source
schema/conduit-packet.schema.json           EDIT 2 $defs + 2 oneOf entries
conduit/schema/conduit-packet.schema.json   EDIT (dual copy, must stay byte-identical)

examples/sample-packet/pydantic-validator-hop.json  EDIT drops the fragment workaround
conduit/tests/test_pydantic_validator_smoke.py      EDIT Config residue assertions flip
```

Untouched on purpose: `conduit/surface/*` (binder, contracts, completeness stay
call-shaped), `key_rename.py`, `ast_attr_call.py`, `ast_param_rename.py`.

---

## 2. Wire surface (packet JSON)

### 2.1 `AST_SYMBOL_MOVE`

The pydantic hop, honestly stated — one rule replaces today's fragment
`AST_IMPORT_REWRITE` **and** the `AST_CALL_REWRITE` that exists only to chase the
decorator:

```json
{
  "type": "AST_SYMBOL_MOVE",
  "target_files": ["*.py"],
  "old_symbol": "pydantic:validator",
  "new_symbol": "pydantic:field_validator",
  "reason": "pydantic 2.0 renamed the validator decorator (docs.pydantic.dev/2.0/migration)."
}
```

Module move (name unchanged, home changed) is the same rule:

```json
{
  "type": "AST_SYMBOL_MOVE",
  "target_files": ["*.py"],
  "old_symbol": "pydantic:BaseSettings",
  "new_symbol": "pydantic_settings:BaseSettings",
  "reason": "BaseSettings moved to the pydantic-settings distribution."
}
```

Optional `rewrite_uses` (default `true`) exists for the case where the new symbol is not
call-compatible and the packet wants the import fixed while a `side_effects` entry tells a
human to reshape the call sites.

`module:name` is a single unambiguous token: the `:` is the boundary the engine cannot
infer from `a.b.c`. It is also one string, so scope filters, dedupe, and generated oracles
keep working on a scalar.

### 2.2 `AST_NESTED_CLASS_TO_ASSIGN`

```json
{
  "type": "AST_NESTED_CLASS_TO_ASSIGN",
  "target_files": ["*.py"],
  "nested_class": "Config",
  "within_base": "pydantic:BaseModel",
  "assign_to": "model_config",
  "wrap_symbol": "pydantic:ConfigDict",
  "keys": {
    "orm_mode": "from_attributes",
    "allow_population_by_field_name": "populate_by_name",
    "schema_extra": "json_schema_extra"
  },
  "drop_keys": ["underscore_attrs_are_private"],
  "on_unknown_key": "refuse",
  "reason": "pydantic 2.0 replaced inner class Config with model_config = ConfigDict(...)."
}
```

```
class User(BaseModel):          →     class User(BaseModel):
    name: str                            name: str
    class Config:                        model_config = ConfigDict(from_attributes=True)
        orm_mode = True
```

Nothing in that rule names pydantic to the engine. `nested_class`, `assign_to`,
`wrap_symbol`, and `keys` are the whole transform; `wrap_symbol` is a `Symbol`, so the
engine imports it through the same ledger that moves symbols (the packet does **not**
carry a second "also add this import" rule).

`on_unknown_key` ∈ `refuse | split | drop`:

| value | a key not in `keys`/`drop_keys` | residue |
|-------|--------------------------------|---------|
| `refuse` (default) | nothing is rewritten for that class | advisory, non-blocking |
| `split` | mapped keys move to the assignment, the nested class survives holding the rest | advisory listing survivors |
| `drop` | unmapped keys are deleted | advisory listing deletions |

Values are **preserved verbatim** (the CST node is moved, not re-rendered). A migration
that must change a *value* (`orm_mode = True` → `mode="before"`) is not this rule; it is
`side_effects`. That boundary is what keeps the rule from becoming a mini-language.

### 2.3 What the schema gains

```jsonc
"ast_symbol_move": {
  "type": "object",
  "required": ["type", "target_files", "old_symbol", "new_symbol"],
  "additionalProperties": false,
  "properties": {
    "type": { "const": "AST_SYMBOL_MOVE" },
    "target_files": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "old_symbol": { "type": "string", "pattern": "^[A-Za-z_][\\w.]*:[A-Za-z_]\\w*$" },
    "new_symbol": { "type": "string", "pattern": "^[A-Za-z_][\\w.]*:[A-Za-z_]\\w*$" },
    "rewrite_uses": { "type": "boolean", "default": true },
    "reason": { "$ref": "#/$defs/rule_reason" }
  }
},
"ast_nested_class_to_assign": {
  "type": "object",
  "required": ["type", "target_files", "nested_class", "assign_to", "keys"],
  "additionalProperties": false,
  "properties": {
    "type": { "const": "AST_NESTED_CLASS_TO_ASSIGN" },
    "target_files": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "nested_class": { "type": "string", "pattern": "^[A-Za-z_]\\w*$" },
    "within_base": { "type": "string", "pattern": "^[A-Za-z_][\\w.]*:[A-Za-z_]\\w*$" },
    "assign_to":   { "type": "string", "pattern": "^[A-Za-z_]\\w*$" },
    "wrap_symbol": { "type": "string", "pattern": "^[A-Za-z_][\\w.]*:[A-Za-z_]\\w*$" },
    "keys": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": { "type": "string", "pattern": "^[A-Za-z_]\\w*$" },
      "propertyNames": { "pattern": "^[A-Za-z_]\\w*$" }
    },
    "drop_keys": { "type": "array", "items": { "type": "string" } },
    "on_unknown_key": { "enum": ["refuse", "split", "drop"], "default": "refuse" },
    "reason": { "$ref": "#/$defs/rule_reason" }
  }
}
```

Both are appended to `$defs/rule.oneOf` in **both** schema copies, and both join
`SDK_RULE_TYPES` and the `engine.py` deferred-impact-path skip set — the two closed sets
plus the elif, per HOW.md gotcha 1.

`AST_IMPORT_REWRITE` stays wire-legal forever (frozen packets must keep applying) but
stops being the engine's vocabulary and stops being minted.

---

## 3. Lowering: wire dicts stop crossing module boundaries

```python
# conduit/packet/lower.py

@dataclass(frozen=True)
class Symbol:
    """An importable name and the module that owns it. `level` = relative-import dots."""
    module: str
    name: str
    level: int = 0

    @property
    def wire(self) -> str: ...        # "pydantic:field_validator"
    def dotted(self) -> str: ...      # "pydantic.field_validator"


def parse_symbol(raw: str) -> Symbol:
    """`"pkg.sub:Name"` -> Symbol. Raises SymbolSyntaxError on anything else."""
    raise NotImplementedError


Severity = Literal["error", "warn"]


@dataclass(frozen=True)
class Diagnostic:
    rule_index: int
    rule_type: str
    severity: Severity
    message: str


@dataclass(frozen=True)
class Plan:
    ops: tuple[Op, ...]
    diagnostics: tuple[Diagnostic, ...]

    def errors(self) -> tuple[Diagnostic, ...]: ...
    def ops_for(self, rel: str) -> tuple[Op, ...]: ...   # target_files glob match


def lower_packet(packet: Mapping[str, Any]) -> Plan:
    """Frozen wire rules -> typed ops. Pure; no filesystem, no LLM, no reminting."""
    raise NotImplementedError
```

Legacy `AST_IMPORT_REWRITE` desugar table (the only place the old string shape is
interpreted):

| `old_import` / `new_import` shape | lowers to | severity |
|---|---|---|
| both parse as `from M import N [as a]` (single name) | `SymbolMoveOp(M:N → M':N')` | — |
| both are dotted module paths (`openai.error` → `openai._exceptions`) | `ModuleRenameOp` | — |
| both parse as `import M [as a]` | `ModuleRenameOp` | — |
| multi-name statement (`from x import A, B`) | one `SymbolMoveOp` per positionally paired name | `warn` |
| clause fragment (`"BaseModel, validator"`), or any other text | *nothing* | **`error`** |

The fragment case is an error, not a silent skip, because the behavior it used to buy —
the unguarded `str.replace` — is deleted in this design. `conduit apply` fails closed on
`Plan.errors()` with the message *"`AST_IMPORT_REWRITE` clause fragments are no longer
executable; express this as `AST_SYMBOL_MOVE` (`module:name`)"*. That is a deliberate,
loud break of exactly one shipped fixture (§8), not a silent behavior change in the field.

---

## 4. `ImportLedger` — the deep module

Every statement that can carry a name lives behind one interface. `SymbolMoveOp` uses it
to move names; `NestedClassToAssignOp` uses it to guarantee `ConfigDict` is importable.
Neither op contains a `cst.ImportFrom`.

```python
# conduit/patcher/py/imports.py

BindingKind = Literal["from_import", "module_import", "star_import"]
GuardKind   = Literal["none", "type_checking", "try_except", "conditional", "local_scope"]


@dataclass(frozen=True)
class Binding:
    """One statement-level fact: `local` currently names `symbol` in this module."""
    local: str
    symbol: Symbol
    kind: BindingKind
    aliased: bool          # `as` present: renaming the symbol must NOT rename use sites
    guard: GuardKind
    lineno: int


RefusalKind = Literal[
    "star_import", "relative_move", "guarded_import", "ambiguous_binding", "unparseable",
]


@dataclass(frozen=True)
class Refusal:
    kind: RefusalKind
    lineno: int
    detail: str


@dataclass(frozen=True)
class Rebind:
    """Result of asking the ledger to move a symbol."""
    bindings_changed: int
    renamed_locals: tuple[tuple[str, str], ...]  # (old_local, new_local) for unaliased only
    refusals: tuple[Refusal, ...]


class ImportLedger:
    """Sole owner of Import / ImportFrom surgery for one parsed module.

    Invariants it maintains, so no op has to:
      * at most one `from M import ...` statement is grown per module per commit;
      * an alias (`as v`) survives a rename -> the op must not touch use sites;
      * a clause that loses its last name loses its statement, and that statement's
        leading comments reattach to the following statement;
      * moving a name that is already imported at the destination *drops* the old alias
        instead of duplicating it  (this is what makes re-apply a no-op);
      * refusals never half-edit: a refused binding leaves the file byte-identical.
    """

    def __init__(self, wrapper: cst.MetadataWrapper) -> None:
        raise NotImplementedError

    def bindings_of(self, symbol: Symbol) -> tuple[Binding, ...]:
        """Every local name that currently resolves to `symbol` (incl. `import pkg`)."""
        raise NotImplementedError

    def rebind(self, old: Symbol, new: Symbol) -> Rebind:
        """Move every binding of `old` to `new`; see §4.1 for the split policy."""
        raise NotImplementedError

    def ensure(self, symbol: Symbol) -> Binding:
        """Return a local name for `symbol`, inserting a minimal import if absent."""
        raise NotImplementedError

    def commit(self) -> cst.Module:
        """Materialize queued statement edits. Idempotent; safe to call once per op."""
        raise NotImplementedError
```

### 4.1 Split policy (decided, not deferred)

`rebind` when `old.module != new.module`:

1. **refuse** (`star_import`) if the name is only reachable through `from M import *`.
2. **refuse** (`relative_move`) if the old binding is relative (`level > 0`) and the new
   symbol is absolute — package-root resolution is repo-layout-specific and guessing it
   silently is worse than a checklist line.
3. **refuse** (`guarded_import`) if `guard == "try_except"` — `try: from pydantic import
   BaseSettings / except ImportError:` is a deliberate compat shim; moving a name out of
   it changes runtime behavior. `guard == "type_checking"` is **allowed** (it is a plain
   `if TYPE_CHECKING:` block and the move is semantics-preserving).
4. Otherwise: drop the `ImportAlias` from the old clause (removing the statement when it
   empties), then either merge into an existing module-level
   `from new.module import ...` or insert a new statement at the old statement's
   position, carrying the `as` alias across unchanged.

Same-module rename: replace the alias's `name` in place. If the destination name is
already present in the same clause, drop the old alias instead (the merge case).

### 4.2 Use-site rewriting

`SymbolMoveOp` rewrites uses only when the ledger says it is safe:

* `aliased=True` → local name unchanged → **no** use-site edit (this is where the string
  fallback got it wrong).
* `kind="from_import"`, unaliased → rename `cst.Name` nodes that libcst's `ScopeProvider`
  resolves to that binding. Shadowed locals (`def f(validator): ...`) are skipped by
  construction, not by regex luck.
* `kind="module_import"` → rewrite `pydantic.validator` attribute access, including the
  module part when the module moved (`ensure` the new module import first).

Because every edit is binding-resolved, the `field_field_validator` failure mode is not
"guarded against" — it is unrepresentable.

---

## 5. Ops: one protocol, two modes

```python
# conduit/patcher/ops/__init__.py

class Mode(enum.Enum):
    APPLY = "apply"     # commit the planned source
    PROBE = "probe"     # discard it; "would have changed" becomes a residue


ResidueKind = Literal[
    "still_old",        # the op would still change this file  -> blocking
    "refused",          # the op declined (see Refusal.kind)   -> advisory
    "unparseable",      # file failed to parse                 -> advisory
    "unsupported_language",
]


@dataclass(frozen=True)
class Residue:
    rel: str
    lineno: int
    op_id: str          # f"{rule_index}:{rule_type}"
    kind: ResidueKind
    detail: str

    @property
    def blocking(self) -> bool:
        return self.kind == "still_old"

    def display(self) -> str: ...


@dataclass(frozen=True)
class OpOutcome:
    source: str                      # planned content (== unit.source when unchanged)
    changes: int
    residues: tuple[Residue, ...]


class Op(Protocol):
    op_id: str
    rule_type: str
    target_files: tuple[str, ...]

    def run(self, unit: FileUnit, mode: Mode) -> OpOutcome:
        """Pure w.r.t. the filesystem: never writes. The caller decides what to keep."""
```

```python
# conduit/patcher/py/unit.py

@dataclass
class FileUnit:
    root: Path
    path: Path
    rel: str
    source: str

    def wrapper(self) -> cst.MetadataWrapper | None:
        """Parsed module with ScopeProvider metadata; None (cached) when unparseable."""
        raise NotImplementedError

    def with_source(self, source: str) -> FileUnit: ...
```

```python
@dataclass(frozen=True)
class PlanResult:
    edits: Mapping[str, str]              # rel -> new content (APPLY only)
    residues: tuple[Residue, ...]
    changes_by_op: Mapping[str, int]


def run_plan(*, root: Path, files: Sequence[Path], plan: Plan, mode: Mode) -> PlanResult:
    """Run every op over every matching file, threading each op's output into the next."""
    raise NotImplementedError
```

The single behavioral rule that makes PROBE trustworthy:

> In `Mode.PROBE`, an op that computes `changes > 0` emits one
> `Residue(kind="still_old")` per changed site and returns `source` unchanged.

So the leftover oracle *is* the rewriter. It cannot drift from apply, because it is apply
with the write suppressed.

### 5.1 `SymbolMoveOp`

```python
@dataclass(frozen=True)
class SymbolMoveOp:
    op_id: str
    rule_type: ClassVar[str] = "AST_SYMBOL_MOVE"
    target_files: tuple[str, ...]
    old: Symbol
    new: Symbol
    rewrite_uses: bool = True

    def run(self, unit: FileUnit, mode: Mode) -> OpOutcome:
        # wrapper = unit.wrapper()  -> None: residue(unparseable), return unit.source
        # ledger  = ImportLedger(wrapper)
        # rebind  = ledger.rebind(self.old, self.new)
        # if self.rewrite_uses and rebind.renamed_locals:
        #     tree = _RenameBoundNames(rebind.renamed_locals, scopes).visit(...)
        # tree = ledger.commit()
        # changes = rebind.bindings_changed + use_site_changes
        # residues = refusals-as-advisories (+ still_old per site when mode is PROBE)
        raise NotImplementedError
```

Non-Python targets: the op consults `languages.registry.engine_for(path)`. Python is
implemented here; a JS/Java/Go file yields `Residue("unsupported_language")` rather than a
pretend edit. (Legacy `ModuleRenameOp` keeps routing through the existing engines, so no
current multi-language capability regresses.)

### 5.2 `NestedClassToAssignOp`

```python
UnknownKeyPolicy = Literal["refuse", "split", "drop"]


@dataclass(frozen=True)
class NestedClassToAssignOp:
    op_id: str
    rule_type: ClassVar[str] = "AST_NESTED_CLASS_TO_ASSIGN"
    target_files: tuple[str, ...]
    nested_class: str
    assign_to: str
    keys: Mapping[str, str]                 # old key -> new key; values preserved verbatim
    wrap: Symbol | None = None              # None => plain dict literal
    within_base: Symbol | None = None
    drop_keys: frozenset[str] = frozenset()
    on_unknown_key: UnknownKeyPolicy = "refuse"

    def run(self, unit: FileUnit, mode: Mode) -> OpOutcome: ...
```

Per-`ClassDef` algorithm (`_NestedClassTransformer.leave_ClassDef`):

```
if within_base and no base of C resolves (via ledger) to within_base:      skip
N = the nested ClassDef named `nested_class` in C.body;  none -> skip
if C.body already binds `assign_to`:        residue("refused", ambiguous_target); skip

entries, strays = partition(N.body)
    entries: SimpleStatementLine[ Assign(target=Name, value=<any expr>) ]
    strays : defs, nested classes, `if`, augmented/annotated assigns, tuple targets
             (a leading docstring and bare `pass` are neither: they are dropped)

mapped   = [(keys[k], value_node) for k in entries if k in keys]
dropped  = [k for k in entries if k in drop_keys]
unknown  = [k for k in entries if k not in keys and k not in drop_keys]

if strays or (unknown and on_unknown_key == "refuse"):
    residue("refused", f"{C.name}.{nested_class}: {strays + unknown}")
    return C unchanged                      # all-or-nothing: never a half-migrated class

local = ledger.ensure(wrap) if wrap else None
stmt  = f"{assign_to} = {local}(" + ", ".join(f"{k}={value}" for k, value in mapped) + ")"
        # built as CST nodes; `value` is the original node, never re-rendered text

replace N with stmt in place (preserving N's leading comments and blank lines)
if on_unknown_key == "split" and unknown:
    keep N, holding only the unknown entries;      residue("refused", survivors)
```

Idempotency is structural: after a successful run the nested class is gone, so the second
run finds nothing and `changes == 0`. Under `split`, the surviving class holds only
unmapped keys, so the second run refuses it — stable, and the advisory repeats.

---

## 6. Apply, leftovers, Watch

```python
# conduit/patcher/engine.py  (inside the existing per-file / per-rule loop)

elif rule_type in LOWERED_RULE_TYPES:          # {"AST_SYMBOL_MOVE", "AST_NESTED_CLASS_TO_ASSIGN"}
    op = plan.op_for_rule_index(index)
    outcome = op.run(FileUnit(root, path, rel, original), Mode.APPLY)
    updated, count = outcome.source, outcome.changes
    report.advisories.extend(outcome.residues)
    detail = f"[{stage_label}] {rule_type} ({count}x)"
```

The op is *the same object* `run_plan` uses; `run_plan` is just the loop, so apply and
probe cannot diverge.

```python
# conduit/patcher/leftovers.py

def probe_packet_residues(root: Path, packet: dict) -> tuple[Residue, ...]:
    """Run the lowered plan in PROBE mode over the pruned file set. Read-only."""
    raise NotImplementedError


def residues_to_leftovers(residues: Iterable[Residue]) -> list[Leftover]:
    """Adapt blocking residues onto the existing public Leftover shape (compat)."""
    raise NotImplementedError
```

`scan_packet_leftovers` keeps its signature and gains one arm:

```
leftovers = (
    export-delta arms            # unchanged
  + AST_CALL_REWRITE old_callee  # unchanged
  + version-guard arm            # unchanged
  + residues_to_leftovers(blocking residues of probe_packet_residues(...))   # NEW
)  deduped on (rel, lineno, callee)
```

Consequences, all of which the rubric asks for:

* A dirty `from pydantic import BaseModel, validator` is now a leftover. Watch reports
  `bump_dirty` instead of `clean`, and apply fails closed instead of succeeding with a
  broken import.
* An un-migrated `class Config` that the packet *can* codemod is a leftover.
* A `class Config` the packet **cannot** codemod (unknown keys, `refuse`) is an
  **advisory**, not a leftover: Watch stays green and the PR body prints it next to
  `side_effects`. Uncodable gaps remain a checklist, exactly as the constraint requires —
  but now a *generated* checklist keyed to a file and line instead of hand-written prose.

Post-apply, `evaluate_apply_leftovers` re-probes. Because apply just ran, any remaining
`still_old` residue means an op is non-idempotent or was blocked; the gate reports it as
`dirty` with the op id. **Idempotency stops being a review property and becomes a
machine-checked postcondition of every apply run.**

`WatchVerdict` / `ApplyLeftoverVerdict` gain `advisories: tuple[Residue, ...]` and the
JSON grows an `advisories: []` key. Additive; existing CI that reads `status`,
`exit_code`, `leftovers` is unaffected.

The surface binder is deliberately untouched. Its job is *call-site completeness proof*;
teaching it about import clauses would make an already subtle module answer two questions.
Probe residues cover the dirty-import blindness that HOW.md gotcha 5 describes, without
growing the binder.

---

## 7. Mint path

```python
# conduit/packet/synthesize.py

_LLM_RULE_ALLOWED_KEYS["AST_SYMBOL_MOVE"] = frozenset(
    {"type", "target_files", "old_symbol", "new_symbol", "rewrite_uses", "reason"}
)
_LLM_RULE_ALLOWED_KEYS["AST_NESTED_CLASS_TO_ASSIGN"] = frozenset(
    {"type", "target_files", "nested_class", "within_base", "assign_to", "wrap_symbol",
     "keys", "drop_keys", "on_unknown_key", "reason"}
)
_LLM_RULE_FIELD_ALIASES["AST_SYMBOL_MOVE"] = ("old_symbol", "new_symbol")


def upgrade_import_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Mint-time: rewrite a single-name AST_IMPORT_REWRITE into AST_SYMBOL_MOVE.

    Same parser as lower.py's desugar table, run one stage earlier so freshly minted
    packets are already in the deep form and lowering's legacy path only serves packets
    frozen before this change.
    """
    raise NotImplementedError
```

Prompt deltas (`_EVIDENCE_SYSTEM` and the synthesize instructions):

* "A renamed or relocated importable name is `AST_SYMBOL_MOVE` with `module:name` on both
  sides (`pydantic:validator` → `pydantic:field_validator`). Never describe an import as a
  statement string, a clause fragment, or an `EXACT_STRING_REPLACE`."
* "An inner configuration class whose body is only `NAME = <value>` assignments is
  `AST_NESTED_CLASS_TO_ASSIGN` with an explicit `keys` map taken from the evidence. If any
  key's *value* must change, or the body has methods/conditionals, omit that key and put
  it in `side_effects`. Never emit `AST_PARAM_RENAME` with a class name as
  `function_target`."
* Unchanged: multi-statement gaps and non-code ripples still go to `side_effects`.

`scope.py`: `_OLD_TOKEN_KEYS` becomes `old_tokens_for(rule) -> list[str]`, returning
`["pydantic:validator", "validator"]` for a symbol move (so the bare name still matches the
lowercase usage index) and `["Config", "orm_mode", ...]` for a nested-class rule.

`test_gen.oracle_forbidden_tokens`: adds the moved symbol's bare name, and switches from
substring containment to `\b`-anchored matching — otherwise a forbidden `validator` token
would fire on the *correct* `field_validator` output.

A validator rejects `AST_PARAM_RENAME` whose `function_target` matches a `ClassDef` name in
the target tree, with a message pointing at `AST_NESTED_CLASS_TO_ASSIGN` (HOW.md gotcha 7).

---

## 8. What the pydantic fixture becomes

Packet (`examples/sample-packet/pydantic-validator-hop.json`): the fragment
`AST_IMPORT_REWRITE` and the companion `AST_CALL_REWRITE` collapse into one
`AST_SYMBOL_MOVE`; a second rule takes the `Config` side_effect off the checklist:

```json
{ "type": "AST_SYMBOL_MOVE", "target_files": ["*.py"],
  "old_symbol": "pydantic:validator", "new_symbol": "pydantic:field_validator" },
{ "type": "AST_NESTED_CLASS_TO_ASSIGN", "target_files": ["*.py"],
  "nested_class": "Config", "within_base": "pydantic:BaseModel",
  "assign_to": "model_config", "wrap_symbol": "pydantic:ConfigDict",
  "keys": { "orm_mode": "from_attributes" } }
```

Fixture after apply:

```python
from pydantic import BaseModel, ConfigDict, field_validator


class User(BaseModel):
    name: str

    @field_validator("name")
    def check_name(cls, v):
        return v

    model_config = ConfigDict(from_attributes=True)

    def dump(self):
        return self.dict()          # still a side_effect; still Watch-blind
```

`tests/test_pydantic_validator_smoke.py` changes: `_residue_counts`'s `class Config`
assertion flips from `>= 1` to `== 0`; `.dict()` residue stays (it is a member-level
change, out of scope here); the notes field loses its confession about the fragment
workaround.

---

## 9. Rollout

| phase | ships | gate |
|---|---|---|
| 1 | `Symbol`, `lower_packet`, `ImportLedger`, `SymbolMoveOp`, schema ×2, stages, engine elif. Legacy desugar on. String fallback **still on**. | unit tests: multi-name rename, alias preserved, module split, merge-into-existing, star/relative/try refusals, double-apply no-op |
| 2 | `NestedClassToAssignOp` + the three unknown-key policies | unit tests per policy + all-or-nothing property |
| 3 | `Mode.PROBE`, `probe_packet_residues`, leftover arm, advisories in apply/Watch JSON, **delete** the import string fallback, fragment desugar becomes `error` | smoke flip (§8); property test: `apply` then `probe` ⇒ zero blocking residues, on every example packet |
| 4 | mint allowlist/prompts/`upgrade_import_rule`, `scope.old_tokens_for`, oracle word boundaries, `AST_PARAM_RENAME`-as-Config validator | live-mint smoke emits `AST_SYMBOL_MOVE`; author-CLI test rejects the fake Config rule |

Phase 3 is the only one with a compatibility edge, and it is deliberately loud.

## 10. Non-goals

* Member-level moves (`BaseModel.dict` → `BaseModel.model_dump`). `AST_CALL_REWRITE` keeps
  that job; `module:name.member` is a future extension of `parse_symbol`, not today's.
* Value rewriting inside a config block.
* Symbol move for JS/TS/Java/Go (explicit `unsupported_language` residue).
* Any change to `conduit/surface/*` — completeness stays a call-site proof.
