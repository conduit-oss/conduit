# Rationale — Candidate 4: symbol-identity ops + one executor, two modes

## Problem

Conduit's packet says what changed in a library; apply makes it true in a consumer tree;
Watch says whether it is still true. Two common migrations break that chain today.

**Import lists.** `AST_IMPORT_REWRITE` carries two opaque strings and rewrites module
paths only — `leave_ImportFrom` never walks `names`. `from pydantic import BaseModel,
validator` with a rule saying `from pydantic import validator` matches neither the module
node nor the source line, so it falls through to an unguarded `str.replace`. The shipped
fixture works around this by putting the clause fragment `"BaseModel, validator"` on the
wire — the packet describes a substring of a source line rather than a fact about the
library. And the fallback has no `old in new` guard, so the honest spelling (`validator`)
would produce `field_field_validator` on the second apply. The workaround exists *because*
the guard can't.

**Nested config.** There is no `leave_ClassDef` or class-body `Assign` rewriter anywhere
in the patcher. `KEY_RENAME` is for quoted keys; `AST_PARAM_RENAME` matches call kwargs
and its `function_target: "Config"` spelling is schema-legal and semantically wrong. So
`class Config: orm_mode = True` is prose in `side_effects`, and the smoke test *asserts
the residue survives*.

Underneath both: the packet's nouns are **syntax fragments**, not **declarations**, and
the verifier is a **separate** hand-written oracle (`ast.Call` vs `AST_CALL_REWRITE.
old_callee`) that structurally cannot see either miss. Watch reports `clean` over a broken
import. Fixing the two transforms without fixing that split just adds two more things the
oracle is blind to.

## Usage

### README, rule-type table (the diff a packet author reads)

> | Type | What it changes |
> |---|---|
> | `AST_SYMBOL_MOVE` | An importable name that was renamed or moved modules. Conduit owns the import clause, the `as` alias, the split when the module changes, and the unaliased use sites. |
> | `AST_NESTED_CLASS_TO_ASSIGN` | An inner configuration class whose body is plain `NAME = value` assignments, rewritten to `target = Wrapper(**renamed_keys)`. All-or-nothing per class. |
>
> ```json
> { "type": "AST_SYMBOL_MOVE", "target_files": ["*.py"],
>   "old_symbol": "pydantic:validator", "new_symbol": "pydantic:field_validator" }
> ```
>
> Write the symbol as `module:name`. Do not write import statements, clause fragments, or
> `EXACT_STRING_REPLACE` for imports — Conduit resolves bindings, so `validator` being a
> substring of `field_validator` is not your problem.

### Call site 1 — a packet author covering the pydantic hop

Today: two rules plus a lie (fragment `old_import` to make the string fallback hit) plus a
`side_effects` line apologizing for `class Config`. After: one `AST_SYMBOL_MOVE` (which
also retires the `AST_CALL_REWRITE` that existed only to chase the decorator) and one
`AST_NESTED_CLASS_TO_ASSIGN` with `{"orm_mode": "from_attributes"}`. The author writes
what the migration guide says and nothing about Python syntax.

### Call site 2 — `conduit apply`

```python
plan = lower_packet(packet)              # once, before any file is touched
if plan.errors():
    fail_closed(plan.errors())           # e.g. a legacy clause fragment, now unexecutable
...
outcome = op.run(FileUnit(root, path, rel, original), Mode.APPLY)
updated, count = outcome.source, outcome.changes
report.advisories.extend(outcome.residues)
```

Apply stops passing `dict[str, Any]` into the patcher. The rule's fields were validated
and parsed once; `SymbolMoveOp.old.module` cannot be `None`, and a malformed symbol dies
in lowering with a rule index, not halfway through a file edit.

### Call site 3 — `conduit watch` / the post-apply gate

```python
residues  = probe_packet_residues(root, packet)          # run_plan(..., Mode.PROBE)
leftovers = existing_arms + residues_to_leftovers(r for r in residues if r.blocking)
advisories = tuple(r for r in residues if not r.blocking)
```

The leftover oracle for AST rules is the rewriter with the write suppressed. A dirty
import is `bump_dirty` because the symbol-move op *would still change that line*. A
`class Config` the packet can codemod is a leftover; one it refuses (unknown keys) is an
advisory with a file and line, printed next to `side_effects`. No one hand-writes a second
matcher, so no one can forget to update it.

### Call site 4 — mint

The LLM emits `{"type": "AST_SYMBOL_MOVE", "old_symbol": "pydantic:validator", ...}`. The
prompt can now say *never describe an import as a string* without leaving the model
without an alternative, which is the actual reason the current prompt tolerates statement
strings and the hop smoke tolerates fragments.

## Shape

Three pieces, derived from those call sites.

**1. `Symbol` and lowering.** The usage above wants typed rules at every call site, so
there is exactly one place where wire text becomes engine vocabulary: `lower_packet(packet)
-> Plan(ops, diagnostics)`. `module:name` uses an explicit separator because `a.b.c` does
not tell the engine where the module stops — and the boundary is precisely the information
the import clause needs. Legacy `AST_IMPORT_REWRITE` desugars here (single-name statement
→ symbol move; dotted path → module rename; fragment → an `error` diagnostic), so the old
string shape is interpreted in one function instead of being re-guessed in the transformer,
the scope filter, and the mint prompt.

**2. `ImportLedger` — the deep module.** Everything hard about both features is import
surgery: renaming inside a name list, preserving `as`, splitting a clause when the module
moves, merging into an existing destination clause, deleting an emptied statement,
refusing a `try/except ImportError` shim, and ensuring `ConfigDict` is importable before
the config rule can emit a call to it. One class owns all of it and states its invariants;
both ops are declarative on top. The config rule getting its import for free — rather than
the packet carrying a second "also add this import" rule — is the concrete payoff of
putting the ledger under both.

**3. One executor, two modes.** `Op.run(unit, mode)` never writes. `Mode.APPLY` keeps the
planned source; `Mode.PROBE` discards it and turns `changes > 0` into
`Residue(kind="still_old")`. This is the piece that makes the whole shape different from a
point fix: the verifier can no longer disagree with the rewriter, and idempotency becomes
a machine-checked postcondition — apply, then probe, and any surviving blocking residue is
a bug report naming the op. The residue type also carries the honest middle ground the
current design lacks: `refused` (the op declined, with a reason and a line) is an advisory,
not a failure, so uncodable gaps stay a checklist while becoming *generated* and located.

Decisions this candidate commits to, where the arena expects disagreement:

* **Supersede, don't extend.** `AST_IMPORT_REWRITE` stays wire-legal for frozen packets and
  is desugared; it is no longer minted and no longer the engine's vocabulary. Adding a
  `names` array to it would keep two ways to say one thing and keep the opaque strings.
* **One config rule, not composed `KEY_RENAME`s.** The unit of correctness is the class:
  half a migrated `Config` is worse than none, and only a single rule can be all-or-nothing.
* **Extend the oracle — via probe, not via the binder.** Watch learns about imports and
  config without `conduit/surface/*` learning a second question.
* **Split by default, refuse three named cases** (`star_import`, `relative_move`,
  `guarded_import`) rather than refusing all module moves or guessing at compat shims.

### Self-screen

*Shallow modules:* the ledger hides ~6 CST edge cases behind 4 methods; the two ops hide
binding resolution and all-or-nothing partitioning behind a 4-field and a 7-field rule.
*Pass-throughs:* `residues_to_leftovers` is the one adapter, and it exists to keep the
public `Leftover`/JSON contract stable — it is a boundary, not a relay. `run_plan` is a
loop, not a wrapper around a single call.
*Information leakage:* wire dicts stop after `lower.py`; `cst` stops inside `patcher/py/`;
no module besides `lower.py` knows what `old_symbol` is spelled like.
*Temporal decomposition:* the design is not "parse, then rename, then fix imports" — ops
are organized by *what they know* (a symbol identity; a declaration reshape), and both call
into the same ledger rather than running in a fixed sequence of phases.

## Tradeoffs

* **A shipped fixture breaks, loudly.** Deleting the unguarded string fallback makes the
  hop packet's clause fragment unexecutable, so it becomes a lowering `error` and the
  example packet plus its smoke assertions must change in the same PR. The alternative —
  keeping the fallback — keeps an unguarded `str.replace` that can produce
  `field_field_validator`, next to a new mechanism whose entire selling point is that it
  cannot. Two mechanisms, one of them known-broken, is worse than one migration.
* **Watch gets stricter, and repos that were green go red.** That is the intent (they were
  green over dirty imports), but it lands as CI failures in consumer repos on upgrade. The
  `refused`/advisory split limits the blast radius to residue the packet actually claims it
  can fix.
* **Probe costs a parse per (op × file).** Watch already parses the pruned file set; the
  multiplier is the op count, not the rule count of the whole packet. If it bites,
  `FileUnit.wrapper()` caches per file and ops are cheap after that — but this is a real
  cost, paid to delete a class of oracle drift.
* **`module:name` is a new spelling an LLM can get wrong.** Mitigated by a schema `pattern`
  (a malformed symbol fails validation rather than applying wrongly), by
  `upgrade_import_rule` accepting the statement form at mint time, and by the fact that the
  current alternative is a free-text string with no failure mode at all.
* **More surface in `patcher/`.** Six new modules. The count is real; each has one job, and
  `ast_import_rewrite.py` shrinks to the module-path engine it always actually was.
* **`within_base` is a lexical base-class match**, not type resolution. A model whose base
  is re-exported through a shim won't match and will be silently skipped rather than
  wrongly rewritten — conservative, but it means coverage is not guaranteed.

## Alternatives considered

1. **Add `names: [{old, new}]` to `AST_IMPORT_REWRITE`; new `AST_CONFIG_CLASS_REWRITE`.**
   The smallest diff, and the likely default. Rejected: `old_import` stays opaque, so
   lowering has to keep guessing what the string means; the import rule still can't express
   a module move for one name without a second rule; and it fixes neither the string
   fallback nor the oracle blindness — Watch would still call a dirty import `clean`.
2. **Compose the config transform from `KEY_RENAME` + a new `CLASS_BODY_TO_ASSIGN`.**
   Rejected: renames would land before the reshape with no transaction around them, so a
   refused reshape leaves renamed keys inside a class nobody migrated. Also multiplies the
   packet surface for one conceptual change.
3. **Express both as `SURFACE_DEFINITE_REWRITE` / grow the binder.** Rejected: that type is
   synthetic and call-chain-only, and the binder's contract is call-site completeness
   proof. Teaching it imports and class bodies would make the subtlest module in the tree
   answer two unrelated questions.
4. **Keep the oracle call-only; accept dirty-import-blind Watch.** Rejected on rubric 5,
   but also on cause: the fragment workaround was invented *because* nothing would have
   caught its absence. A new transform with no verifier grows the same workaround culture.
5. **A general CST-pattern rule (match/replace over node shapes).** Maximum generality,
   rejected as a configuration language: unbounded blast radius, no idempotency story, no
   way for mint to know it produced something safe, and no way for the probe to explain a
   residue in a sentence.
6. **Refuse all cross-module moves** (`BaseSettings` → `pydantic_settings`) and route them
   to `side_effects`. Rejected: it is the single most common shape of a major-version
   import migration, and the split is mechanical everywhere except the three named cases
   the ledger refuses.

## Open questions

1. Should `rewrite_uses` default to `true`? It makes `AST_SYMBOL_MOVE` subsume the
   decorator-chasing `AST_CALL_REWRITE` for pure renames, which is the simplification I
   want — but it silently overlaps with a packet that also carries the call rule. Overlap
   is harmless (the second op finds nothing), yet the surface binder still derives its
   contracts from `AST_CALL_REWRITE` only, so dropping that rule loses proof eligibility.
   Likely answer: keep both rules in packets that want proof, and let the binder learn to
   accept a symbol move as a contract source later.
2. Is a lowering `error` the right severity for legacy clause fragments, or should
   `conduit apply` warn and skip for packets frozen before a schema-version bump? Freezing
   is the contract; loudness is the safety. A `schema_version` gate would let both be true.
3. Should `refused` residues be written into the PR body's `side_effects` section verbatim,
   or kept in a separate "Conduit declined" block? They are generated, located, and
   per-run, which is a different object from the packet's authored checklist.
4. Does `AST_NESTED_CLASS_TO_ASSIGN` need an `insert_at` policy (where the assignment lands
   in the class body)? This design replaces the nested class in place, which is minimal-diff
   but puts `model_config` wherever `class Config` happened to be. Conventional placement is
   the top of the body.
5. `split` on unknown keys can produce a file that the target library rejects at import
   time (pydantic v2 errors when both `Config` and `model_config` exist). Should the policy
   be per-rule as designed, or should the engine refuse `split` unless the packet also
   declares it is safe?

## Next implementation step

Phase 1 of §9, narrowed to a walking skeleton:

1. `conduit/packet/lower.py`: `Symbol`, `parse_symbol`, `Diagnostic`, `Plan`,
   `lower_packet` with the desugar table — pure, no I/O, fully unit-testable.
2. `conduit/patcher/py/imports.py`: `ImportLedger` with `bindings_of` / `rebind` / `commit`
   (leave `ensure` for phase 2), implementing same-module rename and the four split steps.
3. `conduit/patcher/ops/symbol_move.py`: `SymbolMoveOp.run` in `Mode.APPLY` only.
4. Wire it: two schema copies, `SDK_RULE_TYPES`, the `engine.py` elif, and the
   deferred-impact-path skip set.

The gating test for step 2 is the table the current code cannot pass:
`from pydantic import BaseModel, validator` → rename in place;
`from pydantic import validator as v` → clause changes, `@v` does not;
`from pydantic import BaseModel, BaseSettings` + a move → clause loses one name, a new
`from pydantic_settings import BaseSettings` appears, `BaseModel` stays;
`from pydantic import field_validator, validator` → old alias dropped, no duplicate;
`try: from pydantic import BaseSettings / except ImportError:` → byte-identical file plus
one refusal; and every one of them run twice with an assertion that the second run is a
no-op.

## Synthesis decision

(orchestrator fills)
