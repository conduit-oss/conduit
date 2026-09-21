# Rubric: import-list + nested-config engine

Success = a design package that makes both moves packet-driven and generic (no `if package == pydantic` in core), while fitting Conduit's existing apply / schema / mint / Watch surfaces.

## Criteria

1. **Import-list completeness** — Renames a symbol inside `from x import A, B` (and aliases) when the packet says so; does not require whole-line equality with `old_import`.
2. **Config/nested transform generality** — Packet can declare an inner-class / config-block rewrite (e.g. `Config` + key map → assignment form); engine executes it without package-specific branches.
3. **Boundary fit** — Extends or clearly supersedes existing rule types (`AST_IMPORT_REWRITE`, `KEY_RENAME`, side_effects) without leaking wire/schema types across patcher/Watch/mint; schema + normalize + leftover detection stay consistent.
4. **Interface depth** — Small public rule surface; complexity (CST matching, merge of multi-name imports, key remaps) stays inside apply/patcher.
5. **Idempotency + dual verdict** — Re-apply is safe; Watch leftovers/completeness know what `still_old` means for renamed imports and transformed config shapes.
6. **Mint path** — LLM/normalize can emit the new forms without inventing fake string rewrites; uncodable gaps still go to `side_effects`.
