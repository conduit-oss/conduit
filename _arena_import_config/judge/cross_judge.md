# Cross-judge: import-list + nested-config engine

## 1. Criterion-by-criterion scores

### Candidate 1 — `AST_SHAPE_REWRITE`

1. **Import-list completeness — 5/5.** Exact `ImportAlias` matching covers combined imports, aliases, drops, and cross-module splits without substring replacement.
2. **Config/nested transform generality — 4/5.** The packet-driven class/target/constructor/key map is generic and safely refuses partial collapse, though the broad “shape” family is less semantically cohesive than a declaration transaction.
3. **Boundary fit — 4/5.** `ShapePlan` cleanly isolates wire decoding and keeps binder contracts call-only, but one heterogeneous rule type weakens the distinction between import and class-declaration invariants.
4. **Interface depth — 4/5.** One discriminated rule and a batched plan hide substantial CST machinery; the public “shape” abstraction is slightly too generic for the two declaration operations it contains.
5. **Idempotency + dual verdict — 5/5.** Apply and residual scanning share obligation identities, exact AST matching is idempotent, and Watch gains packet-declared structural leftovers.
6. **Mint path — 4/5.** The allowlist, prompt, scope-token, and side-effect story is complete, but the proposed name-based heuristic for rejecting fake Config param rules risks false positives.

**Total: 26/30**

### Candidate 2 — `AST_DECLARATION_REWRITE`

1. **Import-list completeness — 5/5.** Source/target symbols, explicit local-binding policy, and a transactional import editor handle multi-name imports, aliases, moves, merges, and deduplication.
2. **Config/nested transform generality — 5/5.** A generic selector, key map, emission shape, import requirement, and explicit refusal policies make class-to-assignment conversion atomic and extensible.
3. **Boundary fit — 5/5.** Wire dictionaries decode once into immutable domain types; schema, engine batching, scope, leftovers, Watch, and legacy import behavior each have a clear owner.
4. **Interface depth — 5/5.** One closed declaration-operation union fronts a single-file planner/transaction while conflict detection, CST trivia, split/merge, and import ensuring remain private.
5. **Idempotency + dual verdict — 5/5.** Source-shape matching, set-like destination imports, atomic refusal, and `LeftoverReport` give apply and Watch explicit structural parity without distorting the binder.
6. **Mint path — 5/5.** Mint emits semantic operations, never infers them from ambiguous strings, routes unsupported bodies to `side_effects`, and deduplicates decoded operations canonically.

**Total: 30/30**

### Candidate 3 — sibling rules + `ResidueCatalog`

1. **Import-list completeness — 5/5.** The dedicated name rule exactly matches exported names, preserves aliases, and supports split/merge module moves.
2. **Config/nested transform generality — 4/5.** The generic key-map transform is capable, but exposing `drop` for unknown keys creates a lossy policy that is too easy to mint or author accidentally.
3. **Boundary fit — 4/5.** Typed rewrite ops and residue specs are a sound boundary, but two new registries/engine branches increase closed-set wiring and leave legacy behavior alongside them.
4. **Interface depth — 3/5.** Two public rule types plus separate apply transformers and a residue scanner expose more concepts and retain more dispatch complexity than necessary.
5. **Idempotency + dual verdict — 4/5.** Typed structural leftovers fix false-clean Watch results, but apply and residue remain separate implementations that can drift, especially around parse failures and refusal cases.
6. **Mint path — 4/5.** Promotion and structured allowlists are strong; the blanket PascalCase `function_target` rejection is a risky shape heuristic, and multi-name statement promotion remains manual.

**Total: 24/30**

### Candidate 4 — `AST_SYMBOL_MOVE` + APPLY/PROBE executor

1. **Import-list completeness — 5/5.** `ImportLedger` is the strongest import implementation proposed: binding-aware alias preservation, split/merge, guarded-import refusal, and exact use-site resolution.
2. **Config/nested transform generality — 3/5.** The packet is generic, but `split` can produce invalid hybrid configuration and `drop` can silently delete semantics; those policies weaken the transaction guarantee.
3. **Boundary fit — 3/5.** Lowering and typed ops are clean, but making symbol move rewrite use sites overlaps `AST_CALL_REWRITE` while surface contracts still derive only from the latter, creating a proof-boundary mismatch.
4. **Interface depth — 4/5.** `ImportLedger` is genuinely deep and reusable, but the overall six-module op framework and per-op executor are larger than this feature requires.
5. **Idempotency + dual verdict — 2/5.** APPLY/PROBE is appealing, but refused declared transforms are non-blocking advisories, so Watch can report clean while the old import/class remains; independent PROBE ops also observe the original source rather than dependent prior-op output.
6. **Mint path — 4/5.** Symbol syntax and lowering are precise, but retiring the companion call rule loses binder proof eligibility, and tree-dependent rejection of fake param rules does not fit ordinary packet normalization cleanly.

**Total: 21/30**

## 2. Totals

1. **Candidate 2: 30/30**
2. **Candidate 1: 26/30**
3. **Candidate 3: 24/30**
4. **Candidate 4: 21/30**

## 3. Recommended base

**Use Candidate 2 as the base.**

Its declaration transaction is the cleanest ownership boundary: both requested moves edit declarations, can reorganize imports, require atomicity, and need a structural “still old” verdict. The decoded operation union is narrow enough to validate strongly but extensible without leaking libcst or packet dictionaries into Watch and patchers.

Maintainability is strongest because one planner owns overlap detection, import cooperation, rendering, and refusal semantics. Extendability is strongest because another proven declaration migration can become a new closed operation without another top-level rule family or independent engine path. Its key invariants are explicit: source and target symbols are not text fragments, unsupported class bodies remain byte-identical, edits commit atomically, destination imports deduplicate, and any retained source declaration is a blocking structural residual.

## 4. Strongest grafts from each loser

### From Candidate 1

- Graft the explicit stable obligation identity/deduplication rule from `ShapePlan`; conflicting rules for the same source declaration should fail before file mutation.
- Graft the strict retirement of bare-name/comma-fragment string fallback from legacy `AST_IMPORT_REWRITE`.

### From Candidate 3

- Graft tagged leftover kinds and additive Watch JSON fields so structural residue is observable without breaking aggregate `leftover_count`.
- Graft the explicit rule that generated token oracles must not substring-match short import names.

### From Candidate 4

- Graft `ImportLedger` as the private Python import editor beneath Candidate 2's declaration planner, especially its guarded-import refusals, comment ownership, destination deduplication, and binding-aware alias handling.
- Graft the property test `apply successful plan; then read-only scan => zero blocking structural residuals`, while retaining Candidate 2's independent read-only matcher rather than making refusal advisory-only.

## 5. Hard rejects / red flags

- **Reject Candidate 4's non-blocking `refused` semantics for packet-declared operations.** If the old declared shape remains, apply/Watch must be dirty; an advisory is appropriate only for work omitted from mechanical rules and recorded in `side_effects`.
- **Reject Candidate 4's proposal to replace a proof-bearing `AST_CALL_REWRITE` with `AST_SYMBOL_MOVE` while the binder remains call-rule-only.** That silently loses completeness evidence.
- **Reject Candidate 3 and 4's `drop` policy for unknown config keys.** Silent deletion is not a safe generic migration primitive; unknown semantics must refuse atomically or remain authored `side_effects`.
- **Reject any bare-identifier or clause-fragment text fallback for import names.** It is neither alias-aware nor idempotent.
- **Reject heuristic coercion based only on names such as `Config`, `Meta`, or PascalCase.** Schema/domain shape and evidence should decide whether a rule is valid.

## 6. Disagreement with “smallest diff” bias

The smallest patch would extend `AST_IMPORT_REWRITE`, add a standalone class transformer, and leave Watch mostly call-shaped. That is the wrong optimization: it preserves overloaded import strings, duplicates matching semantics, and allows apply/Watch drift.

Candidate 2's modest engine restructuring is justified because batching is part of correctness, not cleanup. The config conversion and import-member move may both mutate the same import table; planning and committing them together prevents order-dependent results and gives one place to enforce atomicity. Prefer that boundary over fewer changed files.
