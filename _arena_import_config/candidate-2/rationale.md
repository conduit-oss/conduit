# Rationale: declaration transactions

## Problem

Conduit's caller contract is a frozen migration packet: the README presents
`conduit run/apply` as deterministic mutation and `conduit watch` as its read-only CI
gate. That contract is currently false for two declaration-shaped migrations. An import
rule cannot name one exported member in a multi-name `ImportFrom`, and no rule can
atomically replace a nested config class with an assignment. Worse, Watch can declare
the tree clean because it only proves call/decorator obligations.

The missing abstraction is not two more visitor callbacks. Both requests rewrite
declarations, can introduce or reorganize imports, require exact source-shape matching,
and need the same matcher to define "still old" for Watch. Treating them as declaration
transactions gives callers one deep interface and gives apply and Watch one semantics.

## Usage

The top-level README establishes three relevant caller promises:

1. A maintainer authors a frozen packet; consumer apply runs without an LLM.
2. `conduit run --path … --packet …` performs deterministic AST/string codemods.
3. `conduit watch --path … --packet …` gates the same hop read-only.

This design lets a packet maintainer express intent without source-line templates:

```json
{
  "type": "AST_DECLARATION_REWRITE",
  "target_files": ["*.py"],
  "operation": {
    "kind": "import_member",
    "source": {"module": "pydantic", "name": "validator"},
    "target": {"module": "pydantic", "name": "field_validator"},
    "local_binding": "preserve"
  }
}
```

The consumer's command does not change. `conduit run` applies this inside
`from pydantic import BaseModel, validator as validates`, preserving the local
`validates` binding. A second run makes no change. `conduit watch` fails after the pin
bump if that source member remains, aliased or not.

The nested-class operation is similarly declarative. The author supplies the class
selector, key map, output assignment/constructor, and optional import requirement.
The consumer still invokes only run/apply or Watch. If a matching class contains a
method or another unsupported member, apply leaves the entire class unchanged and Watch
reports a structural residual. The packet's `side_effects` explains the manual work.

Three existing call sites derive the required boundary:

- `patcher.engine.apply_packet` already partitions frozen rules and owns candidate-file
  selection. It should batch decoded declaration rules per file, not expose CST details
  to CLI callers.
- `patcher.leftovers.scan_packet_leftovers` is shared by apply and Watch. It should
  compose call leftovers with structural residuals produced by the declaration matcher.
- `packet.synthesize.normalize_llm_rule` is the trust boundary for minted JSON. It
  should retain only the closed nested operation and default file targets, never infer a
  structural rule from ambiguous `old`/`new` strings.

These usages lead to immutable decoded rule types, a file-level planning interface, and
a residual report shared by mutation and observation.

## Shape

The packet adds one `AST_DECLARATION_REWRITE` type with a closed `operation` union:
`import_member` and `inner_class_to_assignment`.

This is a semantic rather than syntactic interface. `import_member` identifies source
and target symbols separately from the local binding. Therefore aliases are handled
correctly, a module move is explicit, and an old name cannot accidentally match a
substring. The Python backend decides whether to edit, split, merge, or create an
`ImportFrom`; that complexity is hidden from packet authors.

`inner_class_to_assignment` is intentionally one operation rather than a class rule plus
independent key renames. Conversion is valid only as a transaction: values must be
harvested, keys renamed, the class removed, an assignment emitted, and possibly a
constructor import ensured. Partial execution would leave invalid hybrid state. The
selector is generic (`inner_name`, optional parent base spellings), and the output is
data (`target`, `constructor`, `ensure_import`). Core never branches on package.

The wire format is decoded once into frozen dataclasses. Schema closes the JSON surface;
the decoder enforces cross-field invariants that JSON Schema cannot conveniently state.
Patcher and Watch depend on domain values, not raw packet dictionaries.

Apply parses each affected file once, compiles all declaration rules against the
original tree, rejects overlaps/refusals, and renders once. This makes import
requirements from config conversion cooperate with explicit import moves. There is no
text fallback. Parse failure or an unsupported class body is visible as a refusal.

Watch uses the same source-shape matcher in read-only mode. A `LeftoverReport` has
separate call and structural arms; the existing surface binder remains responsible only
for call/decorator completeness. Clean means no old declaration shape remains and the
call binder does not block. This preserves the useful binder boundary instead of
pretending nested classes are call observations.

Legacy `AST_IMPORT_REWRITE` remains readable for existing packets and module-path
rewrites, but mint stops emitting it for Python declaration changes. The new rule
supersedes its ambiguous full-statement and fragment conventions.

## Tradeoffs

One top-level rule with nested variants makes the schema deeper. That depth is useful:
it prevents adding a third independent dispatch registry for each declaration shape and
creates one place for parsing, planning, conflict detection, and residual semantics.

Syntactic parent-base matching can miss aliased or indirect inheritance. That is a
deliberate safety bias. The selector may omit parent bases when target files are already
precise, or later gain a binder-backed selector without changing operation semantics.
The engine must not guess package identity.

`local_binding` initially supports only `preserve`. Renaming the local binding would
require rewriting all references and belongs with the surface binder, not an import
editor. Encoding only the safe policy is stronger than exposing a misleading option.

`unsupported_members: "refuse"` rejects some mechanically convertible classes. This
protects atomicity and makes gaps observable through residuals and `side_effects`.
Broader member support can be added after defining value-preserving semantics.

Batching declaration rules requires a modest `apply_packet` restructuring instead of
one more `_apply_rules_to_files` branch. In return it avoids repeated CST parses and
order-dependent import edits.

Changing `scan_packet_leftovers` from a list to `LeftoverReport` affects internal callers
and tests. A compatibility adapter can preserve aggregate iteration temporarily, but
the richer type is preferable because callers need honest counts and reasons for both
proof systems.

## Alternatives considered

### Extend `AST_IMPORT_REWRITE` and add `AST_CONFIG_REWRITE`

This is the shortest implementation, but preserves opaque string semantics for imports,
duplicates matching logic between apply and Watch, and adds another per-rule engine
branch. It solves visitors, not the contract. It also leaves config-introduced imports
order-dependent on separate rules.

### Add fields to `AST_IMPORT_REWRITE`

Making `old_import/new_import` coexist with module/name objects produces a modeful type
whose validity depends on which fields happen to be populated. Existing full-statement,
module-path, and fragment interpretations already demonstrate the cost of that
ambiguity. A new closed semantic rule is easier to validate and mint.

### Compose `KEY_RENAME`, `AST_PARAM_RENAME`, and string replacement

`KEY_RENAME` targets quoted config data, and `AST_PARAM_RENAME` targets call kwargs.
Neither can identify class-body assignments. String replacement cannot atomically prove
class shape or preserve formatting. Composition would leak syntax and temporal ordering
into packets while still giving Watch no reliable old-shape predicate.

### Generic tree-pattern DSL

A universal match/replace AST DSL could express both operations, but it would expose CST
structure, trivia, and list surgery to packet authors. It would be broad and shallow.
The declaration union keeps a small semantic surface while leaving room for additional
declaration operations when a repeated migration shape is proven.

### Put structural observations into the surface binder

The binder's evidence model is calls and decorators tied to export spellings. Imports
and inner config classes have different identity and completeness rules. Expanding the
binder would entangle two proof systems and weaken its types. A composed dual verdict is
more explicit.

### Preserve unsupported class members beside the assignment

Moving methods or control flow out of a nested class changes scope and meaning.
Silently preserving only assignments would lose behavior. Refusing the entire transform
and surfacing a residual is safer and deterministic.

## Open questions

1. Should legacy Python `AST_IMPORT_REWRITE` lose its raw fallback immediately, or only
   when packets declare a new schema version?
2. Should `parent_bases_any` remain syntactic in v1, or resolve import aliases through a
   small class-base index before launch?
3. When a moved import carries an inline comment attached to its comma, what exact
   trivia ownership policy should the import editor guarantee?
4. Should an existing assignment to the output target count as an overlap refusal or as
   proof that a previous transform already completed? The conservative answer is:
   source class present plus target assignment present is a conflict; source absent is
   idempotently complete.
5. Does the public Watch JSON need a version bump when adding structured residual rows,
   even if aggregate `leftover_count` remains?

## Next implementation step

Build the decoder and Python planner behind tests before wiring engine dispatch. Start
with golden fixtures for aliased/moved multi-imports, safe config conversion, refusal,
and read-only residual parity. Then update both schema copies, register the single SDK
rule, batch it in `apply_packet`, compose `LeftoverReport`, and finally enable mint.

## Synthesis decision

Chosen as arena **base** (cross-judge 30/30). Grafted: C1 obligation identity + kill fragment fallback; C3 tagged leftover kinds; C4 `ImportLedger` privately under this planner (blocking residuals retained — not C4 advisory-only refusals / CALL subsumption). Full record: `../synthesis/SYNTHESIS.md`.

