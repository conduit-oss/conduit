# Candidate 2: declaration transactions

## Thesis

Replace one-rule/one-transformer dispatch for declaration-shaped edits with one closed,
typed `AST_DECLARATION_REWRITE` packet rule. Its `operation` is a discriminated union:
`import_member` or `inner_class_to_assignment`. Both compile into an internal
`DeclarationPlan`, and the same plan drives apply and read-only residual detection.

This is deliberately not an extension of the opaque `old_import`/`new_import` strings
and not a package-specific `ClassDef` transformer. The public contract describes source
and target declaration shapes. The Python backend owns import grouping/splitting,
alias preservation, CST trivia, atomic class conversion, and idempotency.

## Data structures first

### Packet wire shapes

```json
{
  "type": "AST_DECLARATION_REWRITE",
  "target_files": ["*.py"],
  "operation": {
    "kind": "import_member",
    "source": {"module": "pydantic", "name": "validator"},
    "target": {"module": "pydantic", "name": "field_validator"},
    "local_binding": "preserve"
  },
  "reason": "validator was renamed"
}
```

Aliases are matched by exported name, not local binding:

```python
from pydantic import BaseModel, validator as validates
# becomes
from pydantic import BaseModel, field_validator as validates
```

The same shape supports module moves. The backend splits a mixed import while preserving
the untouched aliases and relative order:

```json
{
  "type": "AST_DECLARATION_REWRITE",
  "target_files": ["**/*.py"],
  "operation": {
    "kind": "import_member",
    "source": {"module": "pydantic", "name": "BaseSettings"},
    "target": {"module": "pydantic_settings", "name": "BaseSettings"},
    "local_binding": "preserve"
  },
  "reason": "BaseSettings moved packages"
}
```

Config-shaped conversion is one atomic declaration transaction:

```json
{
  "type": "AST_DECLARATION_REWRITE",
  "target_files": ["**/*.py"],
  "operation": {
    "kind": "inner_class_to_assignment",
    "selector": {
      "inner_name": "Config",
      "parent_bases_any": ["BaseModel"]
    },
    "keys": {
      "orm_mode": "from_attributes"
    },
    "emit": {
      "target": "model_config",
      "constructor": "ConfigDict",
      "ensure_import": {
        "module": "pydantic",
        "name": "ConfigDict"
      }
    },
    "unmapped_assignments": "preserve",
    "unsupported_members": "refuse"
  },
  "reason": "configuration moved from inner class to a value"
}
```

Given:

```python
class User(BaseModel):
    class Config:
        orm_mode = True
        extra = "forbid"
```

emit:

```python
from pydantic import ConfigDict

class User(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
```

`parent_bases_any` is optional but, when present, must match a syntactic terminal or
dotted base spelling. It narrows generic rules without teaching core what `BaseModel`
means. `unsupported_members: "refuse"` makes conversion all-or-nothing if the inner
class contains methods, control flow, annotated declarations without values, duplicate
keys, `**` expansion, or other non-assignment statements. A docstring and `pass` are
ignorable. The mint path must put such unrepresentable work in `side_effects`.

### Internal domain types

Wire dictionaries are decoded once at `packet/declaration_rules.py`; patchers and Watch
never inspect packet keys.

```python
# conduit/packet/declaration_rules.py
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, TypeAlias

@dataclass(frozen=True)
class Symbol:
    module: str
    name: str

@dataclass(frozen=True)
class ImportMemberSpec:
    source: Symbol
    target: Symbol
    local_binding: Literal["preserve"]

@dataclass(frozen=True)
class InnerClassSelector:
    inner_name: str
    parent_bases_any: tuple[str, ...]

@dataclass(frozen=True)
class AssignmentEmission:
    target: str
    constructor: str
    ensure_import: Symbol | None

@dataclass(frozen=True)
class InnerClassToAssignmentSpec:
    selector: InnerClassSelector
    key_renames: tuple[tuple[str, str], ...]
    emit: AssignmentEmission
    unmapped_assignments: Literal["preserve", "refuse"]
    unsupported_members: Literal["refuse"]

DeclarationOperation: TypeAlias = ImportMemberSpec | InnerClassToAssignmentSpec

@dataclass(frozen=True)
class DeclarationRule:
    target_files: tuple[str, ...]
    operation: DeclarationOperation
    reason: str | None

def decode_declaration_rule(raw: object) -> DeclarationRule:
    raise NotImplementedError

def declaration_rules(packet: object) -> tuple[DeclarationRule, ...]:
    raise NotImplementedError
```

The decoded types encode four invariants: a source/target symbol is never a statement
fragment; alias behavior is explicit; config output is always assignment-to-constructor;
unsupported class bodies have no lossy policy.

```python
# conduit/patcher/declarations/model.py
from dataclasses import dataclass
from enum import Enum

class ResidualKind(Enum):
    IMPORT_MEMBER = "import_member"
    INNER_CLASS = "inner_class"

@dataclass(frozen=True)
class StructuralResidual:
    rel: str
    line: int
    kind: ResidualKind
    old_shape: str
    reason: str

@dataclass(frozen=True)
class DeclarationEdit:
    rule: DeclarationRule
    matched_lines: tuple[int, ...]

@dataclass(frozen=True)
class DeclarationPlan:
    edits: tuple[DeclarationEdit, ...]
    residuals: tuple[StructuralResidual, ...]

@dataclass(frozen=True)
class RewriteResult:
    content: str
    applied: int
    refused: tuple[StructuralResidual, ...]
```

`DeclarationPlan` is produced from an immutable parse. Edits for one rule are committed
atomically. A refusal is not counted as a change and leaves the original class intact.

## Patcher interfaces

```python
# conduit/patcher/declarations/api.py
from pathlib import Path

def rewrite_declarations(
    path: Path,
    content: str,
    rules: tuple[DeclarationRule, ...],
) -> RewriteResult:
    """Compile and execute declaration edits in one parse."""
    raise NotImplementedError

def scan_declaration_residuals(
    path: Path,
    content: str,
    rules: tuple[DeclarationRule, ...],
) -> tuple[StructuralResidual, ...]:
    """Read-only source-shape scan using the same matcher as apply."""
    raise NotImplementedError
```

```python
# conduit/patcher/declarations/python.py
def compile_python_declarations(
    module: cst.Module,
    rules: tuple[DeclarationRule, ...],
) -> DeclarationPlan:
    raise NotImplementedError

def execute_python_plan(
    module: cst.Module,
    plan: DeclarationPlan,
) -> cst.Module:
    raise NotImplementedError
```

The backend performs a single file transaction:

1. Index `ImportFrom` aliases and direct nested classes.
2. Match all operations against the original CST.
3. Refuse overlapping edits or unsupported class bodies.
4. Rewrite imported member names; if the destination module differs, remove that alias
   from the old statement and merge into/create a destination `ImportFrom`.
5. Convert eligible inner classes and queue `ensure_import` requirements.
6. Satisfy imports through the same import editor, deduplicating exact
   `(module, name, local_binding)` entries.
7. Render once.

There is no raw text fallback for `AST_DECLARATION_REWRITE`. Parse failure is a refusal,
not permission for `str.replace`. Existing `AST_IMPORT_REWRITE` remains as a deprecated
legacy module-path rule for old packets; mint never emits it after this schema version.
Its fallback should separately be restricted to non-Python engines or removed.

Idempotency follows from source-shape matching. After apply, the source import member or
selected inner class no longer exists. Destination imports are set-like and are not
duplicated. If source and target symbols are equal, decode rejects the rule.

### Engine integration

Add `AST_DECLARATION_REWRITE` to `SDK_RULE_TYPES` and the deferred-impact set. Do not add
another per-rule `elif`. In `apply_packet`, group declaration rules by target file and
invoke `rewrite_declarations` once per file before call/attribute rules:

```python
def _apply_declaration_rules(
    *,
    root: Path,
    files: list[Path],
    rules: tuple[DeclarationRule, ...],
    dry_run: bool,
) -> PatchReport:
    raise NotImplementedError
```

This batching is essential: the config transform's `ensure_import` and an explicit
`import_member` move share one import table rather than racing through repeated parses.
The existing `_apply_rules_to_files` handles all other rule families unchanged.

## Schema

Both `schema/conduit-packet.schema.json` and
`conduit/schema/conduit-packet.schema.json` add the same definitions and add
`ast_declaration_rewrite` to `$defs.rule.oneOf`.

```json
{
  "ast_declaration_rewrite": {
    "type": "object",
    "required": ["type", "target_files", "operation"],
    "additionalProperties": false,
    "properties": {
      "type": {"const": "AST_DECLARATION_REWRITE"},
      "target_files": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1
      },
      "operation": {
        "oneOf": [
          {"$ref": "#/$defs/import_member_operation"},
          {"$ref": "#/$defs/inner_class_to_assignment_operation"}
        ]
      },
      "reason": {"$ref": "#/$defs/rule_reason"}
    }
  },
  "symbol_ref": {
    "type": "object",
    "required": ["module", "name"],
    "additionalProperties": false,
    "properties": {
      "module": {"type": "string", "minLength": 1},
      "name": {"type": "string", "pattern": "^[A-Za-z_]\\w*$"}
    }
  },
  "import_member_operation": {
    "type": "object",
    "required": ["kind", "source", "target", "local_binding"],
    "additionalProperties": false,
    "properties": {
      "kind": {"const": "import_member"},
      "source": {"$ref": "#/$defs/symbol_ref"},
      "target": {"$ref": "#/$defs/symbol_ref"},
      "local_binding": {"const": "preserve"}
    }
  },
  "inner_class_to_assignment_operation": {
    "type": "object",
    "required": [
      "kind", "selector", "keys", "emit",
      "unmapped_assignments", "unsupported_members"
    ],
    "additionalProperties": false,
    "properties": {
      "kind": {"const": "inner_class_to_assignment"},
      "selector": {"$ref": "#/$defs/inner_class_selector"},
      "keys": {
        "type": "object",
        "propertyNames": {"pattern": "^[A-Za-z_]\\w*$"},
        "additionalProperties": {
          "type": "string",
          "pattern": "^[A-Za-z_]\\w*$"
        }
      },
      "emit": {"$ref": "#/$defs/assignment_emission"},
      "unmapped_assignments": {"enum": ["preserve", "refuse"]},
      "unsupported_members": {"const": "refuse"}
    }
  }
}
```

`inner_class_selector` requires `inner_name`; optional `parent_bases_any` is a unique
array of dotted identifiers. `assignment_emission` requires identifier `target` and
dotted `constructor`, with optional `ensure_import: symbol_ref`.

Schema cannot express source != target, distinct renamed keys, or constructor/import
binding agreement. `decode_declaration_rule` enforces those semantic constraints and
returns a validation error before apply.

## Mint and normalize

Mint is taught the semantic shapes, not aliases:

```python
_LLM_RULE_ALLOWED_KEYS["AST_DECLARATION_REWRITE"] = frozenset(
    {"type", "target_files", "operation", "reason"}
)
```

`normalize_llm_rule` only defaults `target_files`; it does not turn `old/new` strings
into nested operations. This avoids silently guessing whether a string names a module,
member, class, key, or constructor.

Prompt rules:

- Emit `import_member` only when evidence names source and successor symbols.
- Emit `inner_class_to_assignment` only when evidence identifies the old class shape,
  output assignment/constructor, and key mapping.
- Set `unsupported_members: "refuse"`.
- Put methods, computed declarations, multi-statement setup, or unknown conversion
  semantics in `side_effects`; do not emit `AST_PARAM_RENAME` with `Config` as target.
- Prefer one config operation containing its key map over independent key rules.
- Stop emitting legacy `AST_IMPORT_REWRITE` for Python member changes.

Deduplication uses canonical decoded operations, not JSON serialization:

```python
def declaration_rule_key(rule: DeclarationRule) -> tuple[object, ...]:
    raise NotImplementedError
```

`packet/scope.py` gains a structured hook:

```python
def declaration_old_tokens(rule: DeclarationRule) -> tuple[str, ...]:
    match rule.operation:
        case ImportMemberSpec(source=source):
            return (source.module, source.name, f"{source.module}.{source.name}")
        case InnerClassToAssignmentSpec(selector=selector, key_renames=renames):
            return (selector.inner_name, *(old for old, _ in renames))
    raise AssertionError("closed operation union")
```

The source-usage index should keep a config rule when any selector/key token occurs; it
must not require call observations.

## Leftovers and Watch: dual verdict

Calls and declaration obligations are different proof systems. Do not force declaration
observations into `surface.BindingVerdict`. Extend the shared leftover result:

```python
@dataclass(frozen=True)
class LeftoverReport:
    call_leftovers: tuple[Leftover, ...]
    structural_leftovers: tuple[StructuralResidual, ...]

    @property
    def dirty(self) -> bool:
        return bool(self.call_leftovers or self.structural_leftovers)

def scan_packet_leftovers(root: Path, packet: dict) -> LeftoverReport:
    raise NotImplementedError
```

`scan_packet_leftovers` decodes declaration rules, applies target globs, reads Python
files, and calls `scan_declaration_residuals`. It reports:

- `import_member`: an `ImportFrom` still contains the exact source module/exported name,
  regardless of alias.
- `inner_class_to_assignment`: a selected direct inner class still exists. If apply
  refused it, the reason includes the unsupported member.

`evaluate_apply_leftovers` and `evaluate_watch` fail dirty when either arm is non-empty.
The existing binder still independently decides `incomplete`/`unverified` for call and
decorator contracts. `WatchVerdict.to_dict()` adds `call_leftover_count`,
`structural_leftover_count`, and structured residual rows while retaining aggregate
`leftover_count` for compatibility. User-facing text says "leftover migration
obligation(s)", not "call(s)".

This creates an honest dual verdict:

```text
structural obligations clean
AND call/decorator binding does not block
=> clean
```

No package name participates in matching, apply, or Watch.

## Module map

| Module | Responsibility |
|---|---|
| `packet/declaration_rules.py` | Decode closed wire union into immutable domain types; semantic validation |
| `patcher/declarations/model.py` | Plan, result, and residual value types |
| `patcher/declarations/api.py` | Language-neutral apply/scan boundary |
| `patcher/declarations/python.py` | One-pass libcst index, import editor, class conversion, rendering |
| `patcher/engine.py` | Batch declaration rules per file and merge `PatchReport` |
| `patcher/rule_stages.py` | Register `AST_DECLARATION_REWRITE` as SDK |
| `patcher/leftovers.py` | Compose call and structural residual reports |
| `watch.py` | Gate on aggregate residual report; serialize both arms |
| `packet/synthesize.py` | Allowlist and prompt for the semantic union |
| `packet/scope.py` | Structured old-token extraction |
| dual packet schemas | Closed wire definitions |

## Required tests

1. Rename one member in a parenthesized/commented multi-import; preserve other names.
2. Rename `validator as validates`; preserve `validates`.
3. Move one member to another module; split, merge with an existing target import, and
   preserve comments.
4. Confirm `validator` does not rewrite inside `field_validator`, strings, or comments.
5. Convert a direct inner class, rename mapped keys, preserve unmapped assignments, and
   ensure/deduplicate the constructor import.
6. Refuse a class containing a method atomically; structural leftover blocks apply/Watch.
7. Do not match a same-named inner class whose parent base selector misses.
8. Reapply every successful fixture and assert zero changes.
9. Assert both schema copies accept examples and reject unknown operation fields.
10. Assert mint normalization retains the nested operation and never fabricates it from
    `old`/`new`.
11. At a bumped pin, verify an aliased dirty import and a residual inner class each cause
    `bump_dirty`; after apply, call binder and structural scanner both permit `clean`.

