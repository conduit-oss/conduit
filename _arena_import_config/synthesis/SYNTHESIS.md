# Synthesis: declaration transactions (import-list + nested config)

## Base

**Candidate 2** (`AST_DECLARATION_REWRITE`) — [Cross-judge](e548c342-aba8-4a97-8483-63472f88bd70) 30/30; parent agrees on boundary and hard rejects.

One closed SDK rule with `operation` union (`import_member` | `inner_class_to_assignment`). Wire → immutable domain types once. Batch plan/commit per file so import moves and config `ensure_import` share one import table. Structural leftovers block apply/Watch; binder stays Call-only.

## Grafts

| From | Graft |
|------|--------|
| C1 | Stable obligation identity / conflict-fail before mutate; retire bare-name & clause-fragment string fallback on legacy `AST_IMPORT_REWRITE` |
| C3 | Tagged leftover kinds + additive Watch JSON (`leftover_kinds`); token oracles never substring-match short import names |
| C4 | Private `ImportLedger` under the declaration planner (split/merge, guarded-import refuse, alias preservation, dest dedupe); property test apply→scan ⇒ zero blocking structural residuals — **not** C4's advisory-only refusals or CALL subsumption |

## Hard rejects (locked)

- Packet-declared ops that leave the old shape → **blocking** residual (not advisory-only).
- Do not replace proof-bearing `AST_CALL_REWRITE` with symbol-move while binder is call-only.
- No `drop` unknown-config-key policy; refuse atomically or `side_effects`.
- No bare-id / clause-fragment import string fallback.
- No PascalCase/`Config` name heuristics for mint rejection.

## Wire (author-facing)

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

```json
{
  "type": "AST_DECLARATION_REWRITE",
  "target_files": ["*.py"],
  "operation": {
    "kind": "inner_class_to_assignment",
    "selector": {"inner_name": "Config", "parent_bases_any": ["BaseModel"]},
    "keys": {"orm_mode": "from_attributes"},
    "emit": {
      "target": "model_config",
      "constructor": "ConfigDict",
      "ensure_import": {"module": "pydantic", "name": "ConfigDict"}
    },
    "unmapped_assignments": "refuse",
    "unsupported_members": "refuse"
  }
}
```

Legacy `AST_IMPORT_REWRITE` remains module-path only (mint stops emitting statement/fragment forms).

## Module map (synthesized)

| Path | Owns |
|------|------|
| dual `schema/…conduit-packet.schema.json` | `$defs/ast_declaration_rewrite` + operation oneOf |
| `packet/declaration_rules.py` | Decode wire → frozen domain; semantic validate; obligation identity |
| `patcher/declarations/{model,api,python}.py` | Plan / `rewrite_declarations` / `scan_declaration_residuals` |
| `patcher/py/imports.py` | `ImportLedger` (private) |
| `patcher/ast_import_rewrite.py` | Module-path only; guarded dotted fallback |
| `patcher/rule_stages.py` + `engine.py` | Register + batch declaration rules per file |
| `patcher/leftovers.py` + `watch.py` | `LeftoverReport` call + structural arms; tagged kinds |
| `packet/synthesize.py` | Allowlist, prompts; no ambiguous `old`/`new` inference |

## Verification (arena)

- Rubric 1–6: covered by base + grafts.
- Red flags screened: no shallow pass-through public API; wire stops at decoder; planner owns declaration knowledge; ledger is deep not pass-through.
- Risk accepted: Watch grows stricter for packets that declare these ops; hop fixture fragment must migrate; smokes that assert Config residue must flip when nested op is present.

## Implementation order

1. Schema ×2 + `declaration_rules` decoder + unit tests.
2. `ImportLedger` + `import_member` apply/scan; kill short-id import fallback; hop packet → declaration rule.
3. `inner_class_to_assignment` + structural leftovers in apply/Watch.
4. Mint allowlist/prompts; live remint path later.
