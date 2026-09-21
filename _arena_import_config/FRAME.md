# Arena frame: import-list + nested-config engine

## Artifact

Each candidate produces a design package under its `candidate-N/` dir:

- `DESIGN.md` — type sketch, signatures, module map (`not implemented` bodies OK)
- `rationale.md` — shaped per architect `rationale-template.md` (usage first)

## Task (same for all)

Design Conduit engine + packet support for **both**:

1. **Import-list rewrite** — rename a name inside `from x import A, B` (and aliases), not only whole-line/exact module strings. Packet-driven.
2. **Nested config / inner-class transform** — packet declares “rewrite this inner class / config block into an assignment + key renames”; engine runs it generically (e.g. `Config` + `orm_mode→from_attributes` → `model_config = ConfigDict(...)`). **No** `if package == "pydantic"` in core.

Honor `_arena_import_config/GROUNDING.md` and `_arena_import_config/HOW.md`.

## Rubric

See `_arena_import_config/RUBRIC.md`.

## Exhaust the design space

Produce a **whole-shape** alternative, not a point fix inside one obvious shape. Distinct candidates should disagree on at least one of: extend vs new import rule type; one Config rule vs composed KEY/PARAM rules; leftover/Watch contract change vs calls-only; split-import policy.
